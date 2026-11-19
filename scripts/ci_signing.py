"""Configure certificates only on disposable GitHub-hosted build runners."""
import base64
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys


def run(args):subprocess.run(list(map(str,args)),check=True,stdout=subprocess.DEVNULL)


def setup(folder):
    folder.mkdir(mode=0o700,exist_ok=True)
    def required(key):
        value=os.environ.get(key)
        if not value:raise ValueError('Missing signing secret: '+key)
        return value
    def certificate(key,name):
        path=folder/name
        with path.open('wb') as dest:dest.write(base64.b64decode(required(key),validate=True))
        path.chmod(0o600);return path
    values={}
    if sys.platform=='darwin':
        app=certificate('APPLE_CERTIFICATE_BASE64','app.p12')
        password=secrets.token_hex(24);keychain=folder/'signing.keychain-db'
        run(['security','create-keychain','-p',password,keychain])
        run(['security','set-keychain-settings','-lut','21600',keychain])
        run(['security','unlock-keychain','-p',password,keychain])
        run(['security','import',app,'-k',keychain,'-P',required('APPLE_CERTIFICATE_PASSWORD'),'-T','/usr/bin/codesign'])
        run(['security','set-key-partition-list','-S','apple-tool:,apple:,codesign:','-s','-k',password,keychain])
        # This changes only the disposable runner's search list, never a user's Mac.
        run(['security','list-keychains','-d','user','-s',keychain])
        run(['xcrun','notarytool','store-credentials','huginn-ci','--keychain',keychain,
             '--apple-id',required('APPLE_ID'),'--team-id',required('APPLE_TEAM_ID'),'--password',required('APPLE_APP_PASSWORD')])
        values={'HUGINN_APPLE_APP_IDENTITY':required('APPLE_APP_IDENTITY'),
                'HUGINN_NOTARY_PROFILE':'huginn-ci','HUGINN_NOTARY_KEYCHAIN':str(keychain)}
    elif sys.platform=='win32':
        cert=certificate('WINDOWS_CERTIFICATE_BASE64','windows.pfx')
        roots=Path(os.environ['ProgramFiles(x86)'])/'Windows Kits/10/bin'
        tools=sorted(roots.glob('*/x64/signtool.exe'),reverse=True)
        if not tools:raise ValueError('Windows SDK signtool.exe is unavailable')
        values={'HUGINN_WINDOWS_CERT_FILE':str(cert),
                'HUGINN_SIGNTOOL':str(tools[0])}
    else:raise ValueError('Signing is supported only on Windows/macOS')
    with Path(os.environ['GITHUB_ENV']).open('a',encoding='utf-8') as dest:
        for key,value in values.items():
            if '\n' in value or '\r' in value:raise ValueError('Multiline signing setting is not supported')
            dest.write(key+'='+value+'\n')


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise ValueError('Use only on a disposable GitHub-hosted runner; configure local identities yourself')
    folder=Path(os.environ['RUNNER_TEMP'])/'huginn-signing'
    if sys.argv[1]=='setup':setup(folder)
    elif sys.argv[1]=='cleanup':
        keychain=folder/'signing.keychain-db'
        if sys.platform=='darwin' and keychain.exists():run(['security','delete-keychain',keychain])
        if folder.exists():shutil.rmtree(folder)
    else:raise ValueError('Expected setup or cleanup')


if __name__=='__main__':
    try:main()
    except subprocess.CalledProcessError as error:
        print('Signing setup failed in '+Path(str(error.cmd[0])).name+' (exit '+str(error.returncode)+')',file=sys.stderr);sys.exit(1)
    except (ValueError,KeyError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
