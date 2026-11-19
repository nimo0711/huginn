"""Small release contracts shared by local builds and CI."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import zipfile


SOURCE_FILES = (
    'desktop_service.py', 'runtime_paths.py', 'platform_support.py',
    'server.py', 'scan.py', 'usage.py', 'sprint.py', 'enrich.py', 'connections.py',
    'board_actions.py', 'session_delivery.py', 'local_runtime.py', 'branding.py',
    'blocks.py', 'progress.py', 'delegation.py', 'title_summary.py',
    'wbcore.py', 'actions_cli.py', 'claude_hook.py', 'install_claude_hooks.py', 'wb',
    'index.html', 'stories.html', 'AGENT_GUIDE.md', 'release.json',
    'desktop/theme.css',
    'app/brand.json', 'app/assets/forkrail-icon.png', 'app/assets/forkrail-icon-web.png',
)
TARGETS = {'macos-arm64': ('darwin','arm64'), 'macos-x86_64': ('darwin','amd64'),
           'windows-x64': ('windows','amd64')}
MACHO_MAGIC={b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',
             b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf',b'\xbf\xba\xfe\xca'}


def check_macos_minimum(load_commands, name, maximum):
    versions=re.findall(r'\bminos (\d+(?:\.\d+){1,2})',load_commands)
    versions+=re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+){1,2})',load_commands)
    if not versions:raise ValueError('Cannot establish minimum macOS version: '+str(name))
    limit=tuple(maximum)+(0,)*(3-len(maximum))
    for version in versions:
        parts=tuple(map(int,version.split('.')));parts+=(0,)*(3-len(parts))
        if parts>limit:raise ValueError(str(name)+' requires macOS '+version+', beyond the supported minimum '+'.'.join(map(str,maximum)))


def verify_macos_bundle(app, target, minimum=(13,0)):
    arch='arm64' if target=='macos-arm64' else 'x86_64';count=0
    for path in Path(app).rglob('*'):
        if path.is_symlink() or not path.is_file():continue
        with path.open('rb') as stream:magic=stream.read(4)
        if magic not in MACHO_MAGIC:continue
        arches=subprocess.check_output(['lipo','-archs',str(path)],text=True).split()
        if arch not in arches:raise ValueError('Missing '+arch+' slice: '+str(path))
        commands=subprocess.check_output(['otool','-arch',arch,'-l',str(path)],text=True)
        check_macos_minimum(commands,path,minimum);count+=1
    if not count:raise ValueError('No native executable in the app')
    print('Verified '+str(count)+' Mach-O files: '+arch+', macOS '+'.'.join(map(str,minimum)))


def validate_config(value, tag=None):
    if not isinstance(value,dict):raise ValueError('release.json must be an object')
    if value.get('name') != 'Forkrail':raise ValueError('Invalid product name')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-(?:rc|beta|alpha)\.\d+)?',value.get('version','')):
        raise ValueError('Invalid release version')
    if not re.fullmatch(r'\d+\.\d+\.\d+',value.get('package_version','')):
        raise ValueError('Invalid installer version')
    if value['package_version']!=value['version'].split('-')[0]:
        raise ValueError('Installer version does not match release')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+',value.get('bundle_id','')):
        raise ValueError('Invalid bundle identifier')
    repository=value.get('repository','')
    if not isinstance(repository,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',repository):
        raise ValueError('Invalid GitHub release repository')
    if value.get('platforms')!=list(TARGETS):raise ValueError('Unknown release platforms')
    if tag is not None and tag!='v'+value['version']:
        raise ValueError('Tag must match release.json: v'+value['version'])
    return value


def config(root, tag=None):
    return validate_config(json.loads((Path(root)/'release.json').read_text(encoding='utf-8')),tag)


def stage_sources(root, destination):
    root=Path(root).resolve();destination=Path(destination)
    # Validate the complete input before copying anything.
    for name in SOURCE_FILES:
        source=root/name
        if source.is_symlink() or any(p.is_symlink() for p in source.parents if p!=root and root in p.parents):
            raise ValueError('Distribution source must not be a symlink: '+name)
        if not source.is_file():raise FileNotFoundError(source)
    destination.mkdir(parents=True,exist_ok=True)
    for name in SOURCE_FILES:
        target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/name,target)


def validate_signing(target, signed, env):
    if target not in TARGETS:raise ValueError('Unknown target')
    if not signed:return
    required=(['FORKRAIL_APPLE_APP_IDENTITY','FORKRAIL_NOTARY_PROFILE']
              if target.startswith('macos') else ['FORKRAIL_WINDOWS_CERT_FILE','FORKRAIL_WINDOWS_CERT_PASSWORD'])
    missing=[name for name in required if not env.get(name)]
    if missing:raise ValueError('Signed release requires: '+', '.join(missing))


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def write_checksums(directory, artifacts):
    root=Path(directory).resolve();rows=[]
    for path in sorted(map(Path,artifacts)):
        if path.resolve().parent!=root or not path.is_file() or path.is_symlink():
            raise ValueError('Checksum artifacts must be direct files in the output directory')
        rows.append(sha256(path)+'  '+path.name+'\n')
    if not rows:raise ValueError('No artifacts to checksum')
    (root/'SHA256SUMS').write_text(''.join(rows),encoding='utf-8')


def verify_checksums(directory):
    root=Path(directory).resolve();seen=set()
    for row in (root/'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        match=re.fullmatch(r'([0-9a-f]{64})  ([^/\\\r\n]+)',row)
        if not match or match[2] in ('.','..') or match[2] in seen:raise ValueError('Invalid checksum manifest')
        name=match[2];seen.add(name);path=root/name
        if path.is_symlink() or path.resolve().parent!=root or not path.is_file():raise ValueError('Missing artifact: '+name)
        if sha256(path)!=match[1]:raise ValueError('Checksum mismatch: '+name)
    if not seen:raise ValueError('Empty checksum manifest')
    return seen


def zip_tree(source, destination):
    source=Path(source)
    with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(source.rglob('*')):
            relative=path.relative_to(source.parent).as_posix()
            if path.is_symlink():
                resolved=path.resolve()
                if source.resolve() not in resolved.parents:raise ValueError('External archive symlink')
                info=zipfile.ZipInfo(relative);info.create_system=3
                info.external_attr=(stat.S_IFLNK|0o777)<<16
                archive.writestr(info,os.readlink(path))
            elif path.is_file():archive.write(path,relative)
