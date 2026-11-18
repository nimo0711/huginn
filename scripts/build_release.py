"""Build an installable Forkrail candidate on its target OS. Never publishes."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

from release_tools import TARGETS, MACHO_MAGIC, config, stage_sources, validate_signing, verify_macos_bundle, write_checksums, zip_tree

ROOT=Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    print('Running '+Path(str(args[0])).name,flush=True)
    subprocess.run(list(map(str,args)),check=True,**kwargs)


def native_target():
    system=platform.system();arch=platform.machine().lower()
    if system=='Darwin':return 'macos-arm64' if arch=='arm64' else 'macos-x86_64'
    if system=='Windows' and arch in ('amd64','x86_64'):return 'windows-x64'
    raise ValueError('Build on a supported Windows x64 or macOS runner')


def make_icons():
    from PIL import Image
    folder=ROOT/'desktop/build';folder.mkdir(parents=True,exist_ok=True)
    original=ROOT/'app/assets/forkrail-icon.png'
    shutil.copy2(original,folder/'appicon.png')
    with Image.open(original) as source:
        source.save(folder/'Forkrail.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
        if sys.platform=='darwin':source.save(folder/'Forkrail.icns')


def freeze_service(work, signed):
    source=work/'source';stage_sources(ROOT,source)
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir',
         '--name','forkrail-service','--distpath',work/'frozen','--workpath',work/'pyinstaller',
         '--specpath',work,'--paths',source,'--collect-submodules','psutil']
    for item in ['index.html','stories.html','AGENT_GUIDE.md','release.json','app/brand.json',
                 'app/assets/forkrail-icon.png','app/assets/forkrail-icon-web.png']:
        parent=Path(item).parent.as_posix()
        cmd+=['--add-data',str(source/item)+':'+parent]
    if signed and sys.platform=='darwin':cmd+=['--codesign-identity',os.environ['FORKRAIL_APPLE_APP_IDENTITY']]
    cmd+=[source/'desktop_service.py'];run(cmd,cwd=source)
    service=work/'frozen/forkrail-service'
    binary=service/('forkrail-service.exe' if sys.platform=='win32' else 'forkrail-service')
    run([binary,'self-test'],timeout=60)
    return service


def desktop_build(release,target):
    make_icons()
    settings_path=ROOT/'desktop/wails.json'
    settings=json.loads(settings_path.read_text(encoding='utf-8'))
    settings.setdefault('info',{})['productVersion']=release['package_version']
    settings['info']['productName']=release['name']
    settings_path.write_text(json.dumps(settings,indent=2)+'\n',encoding='utf-8')
    goos,goarch=TARGETS[target]
    cmd=['go','run','github.com/wailsapp/wails/v2/cmd/wails@v2.16.0','build','-clean',
         '-platform',goos+'/'+goarch,'-ldflags','-X main.releaseVersion='+release['version']]
    env=dict(os.environ)
    if goos=='darwin':
        env['MACOSX_DEPLOYMENT_TARGET']='13.0'
        env['CGO_CFLAGS']=env.get('CGO_CFLAGS','')+' -mmacosx-version-min=13.0'
        env['CGO_LDFLAGS']=env.get('CGO_LDFLAGS','')+' -mmacosx-version-min=13.0'
    run(cmd,cwd=ROOT/'desktop',env=env)
    return ROOT/'desktop/build/bin'/('Forkrail.app' if goos=='darwin' else 'Forkrail.exe')


def sign_mac(app, signed):
    identity=os.environ['FORKRAIL_APPLE_APP_IDENTITY'] if signed else '-'
    flags=['--force','--sign',identity]
    if signed:flags+=['--options','runtime','--timestamp']
    for path in sorted(app.rglob('*'),key=lambda p:len(p.parts),reverse=True):
        if path.is_symlink():continue
        if path.is_file():
            with path.open('rb') as f:header=f.read(4)
            if header in MACHO_MAGIC:run(['codesign',*flags,path])
        elif path.suffix=='.framework':run(['codesign',*flags,path])
    run(['codesign',*flags,app]);run(['codesign','--verify','--deep','--strict',app])


def package_mac(release,target,work,service,built,signed,output):
    app=work/'package/Applications/Forkrail.app';app.parent.mkdir(parents=True)
    shutil.copytree(built,app,symlinks=True)
    shutil.copytree(service,app/'Contents/Resources/service',symlinks=True)
    shutil.copy2(ROOT/'desktop/build/Forkrail.icns',app/'Contents/Resources/AppIcon.icns')
    # Wails' XML plist begins with DOCTYPE, without an XML declaration.
    info=app/'Contents/Info.plist';data=plistlib.loads(info.read_bytes(),fmt=plistlib.FMT_XML)
    data.update(CFBundleName=release['name'],CFBundleDisplayName=release['name'],
                CFBundleIdentifier=release['bundle_id'],CFBundleShortVersionString=release['package_version'],
                CFBundleVersion=release['package_version'],CFBundleIconFile='AppIcon',LSMinimumSystemVersion='13.0',
                NSHighResolutionCapable=True,NSHumanReadableCopyright='Forkrail contributors')
    info.write_bytes(plistlib.dumps(data));verify_macos_bundle(app,target);sign_mac(app,signed)
    run([app/'Contents/Resources/service/forkrail-service','self-test'],timeout=60)
    stem=release['name']+'-'+release['version']+'-'+target
    pkg=output/(stem+'.pkg');components=work/'components.plist'
    run(['pkgbuild','--analyze','--root',work/'package',components])
    rows=plistlib.loads(components.read_bytes())
    for row in rows:row.update(BundleIsRelocatable=False,BundleOverwriteAction='upgrade')
    components.write_bytes(plistlib.dumps(rows))
    cmd=['pkgbuild','--root',work/'package','--component-plist',components,
         '--identifier',release['bundle_id'],'--version',release['package_version'],'--install-location','/']
    component=work/'Forkrail-component.pkg'
    run([*cmd,component])
    distribution=work/'Distribution.xml'
    from xml.sax.saxutils import escape
    arch='arm64' if target=='macos-arm64' else 'x86_64'
    distribution.write_text('<?xml version="1.0" encoding="utf-8"?>\n'
        '<installer-gui-script minSpecVersion="2"><title>Forkrail</title>'
        '<options customize="never" require-scripts="false" hostArchitectures="'+arch+'"/>'
        '<volume-check script="true"><allowed-os-versions><os-version min="13.0"/></allowed-os-versions></volume-check>'
        '<must-close><app id="'+escape(release['bundle_id'])+'"/></must-close>'
        '<choices-outline><line choice="default"/></choices-outline>'
        '<choice id="default" visible="false"><pkg-ref id="'+escape(release['bundle_id'])+'"/></choice>'
        '<pkg-ref id="'+escape(release['bundle_id'])+'" version="'+release['package_version']+'">Forkrail-component.pkg</pkg-ref>'
        '</installer-gui-script>',encoding='utf-8')
    product=['productbuild','--distribution',distribution,'--package-path',work]
    if signed:product+=['--sign',os.environ['FORKRAIL_APPLE_INSTALLER_IDENTITY']]
    run([*product,pkg])
    if signed:
        notary=['--keychain-profile',os.environ['FORKRAIL_NOTARY_PROFILE']]
        if os.environ.get('FORKRAIL_NOTARY_KEYCHAIN'):notary+=['--keychain',os.environ['FORKRAIL_NOTARY_KEYCHAIN']]
        run(['xcrun','notarytool','submit',pkg,*notary,'--wait'])
        run(['xcrun','stapler','staple',pkg]);run(['xcrun','stapler','validate',pkg])
        # The app ZIP also needs a ticket; notarizing the pkg alone is insufficient.
        upload=work/'notarize.zip';run(['ditto','-c','-k','--sequesterRsrc','--keepParent',app,upload])
        run(['xcrun','notarytool','submit',upload,*notary,'--wait'])
        run(['xcrun','stapler','staple',app]);run(['spctl','--assess','--type','execute','--verbose',app])
    archive=output/(stem+'.zip')
    run(['ditto','-c','-k','--sequesterRsrc','--keepParent',app,archive])
    return [pkg,archive],app


def sign_windows(path):
    tool=os.environ.get('FORKRAIL_SIGNTOOL') or shutil.which('signtool')
    if not tool:raise ValueError('signtool.exe must be on PATH for signed Windows builds')
    run([tool,'sign','/fd','SHA256','/tr','http://timestamp.digicert.com','/td','SHA256',
         '/f',os.environ['FORKRAIL_WINDOWS_CERT_FILE'],'/p',os.environ['FORKRAIL_WINDOWS_CERT_PASSWORD'],path])
    run([tool,'verify','/pa',path])


def package_windows(release,target,work,service,built,signed,output):
    folder=work/'Forkrail';folder.mkdir()
    shutil.copy2(built,folder/'Forkrail.exe');shutil.copytree(service,folder/'service')
    shutil.copy2(ROOT/'desktop/build/Forkrail.ico',folder/'Forkrail.ico')
    if signed:
        sign_windows(folder/'Forkrail.exe');sign_windows(folder/'service/forkrail-service.exe')
    run([folder/'service/forkrail-service.exe','self-test'],timeout=60)
    stem=release['name']+'-'+release['version']+'-'+target
    installer=output/(stem+'-Setup.exe')
    nsis=shutil.which('makensis') or str(Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'NSIS/makensis.exe')
    run([nsis,'/V2','/DSOURCE='+str(folder),'/DOUTPUT='+str(installer),
         '/DVERSION='+release['package_version'],'/DRELEASE_VERSION='+release['version'],ROOT/'packaging/windows/installer.nsi'])
    if signed:sign_windows(installer)
    archive=output/(stem+'.zip');zip_tree(folder,archive)
    return [installer,archive],folder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',choices=TARGETS);parser.add_argument('--tag')
    parser.add_argument('--signed',action='store_true')
    args=parser.parse_args();target=args.target or native_target()
    release=config(ROOT,args.tag);validate_signing(target,args.signed,os.environ)
    if target!=native_target():raise ValueError('Python and native dependencies must be built on the target OS and CPU')
    source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    source_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
    work=ROOT/'build/release'/target
    if work.is_symlink():raise ValueError('Build directory cannot be a symlink')
    if work.exists():shutil.rmtree(work)
    work.mkdir(parents=True)
    output=ROOT/'dist'/target
    if output.is_symlink():raise ValueError('Output directory cannot be a symlink')
    if output.exists():shutil.rmtree(output)
    output.mkdir(parents=True)
    service=freeze_service(work,args.signed);built=desktop_build(release,target)
    packager=package_mac if target.startswith('macos') else package_windows
    artifacts,app=packager(release,target,work,service,built,args.signed,output)
    manifest={'name':release['name'],'version':release['version'],'target':target,
              'source_commit':source_commit,'source_dirty':source_dirty,
              'signed':args.signed,'artifacts':[p.name for p in artifacts]}
    evidence=output/'release-manifest.json';evidence.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    write_checksums(output,[*artifacts,evidence])
    print(json.dumps({'output':str(output),'app':str(app),'signed':args.signed}))


if __name__=='__main__':
    try:main()
    except subprocess.CalledProcessError as error:
        # Signing command arguments can contain certificate passwords.
        print('Release command failed: '+Path(str(error.cmd[0])).name+' (exit '+str(error.returncode)+')',file=sys.stderr);sys.exit(1)
    except (ValueError,FileNotFoundError) as error:
        print('Release build failed: '+str(error),file=sys.stderr);sys.exit(1)
