"""Create a draft from verified matrix outputs; never publishes a release."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from release_tools import config, sha256
from verify_release import verify
from check_release_request import verify_tag_commit


def prepare_assets(folder, target, destination, signed, commit):
    manifest=verify(folder,signed,expected_target=target,require_smoke=True,expected_commit=commit)
    assets=[Path(folder)/name for name in manifest['artifacts']]
    for name in ['release-manifest.json','smoke-result.json']:
        copied=destination/(target+'-'+name)
        shutil.copy2(Path(folder)/name,copied);assets.append(copied)
    # Use the exact downloadable names, including renamed platform evidence.
    checksums=destination/(target+'-SHA256SUMS')
    checksums.write_text(''.join(sha256(path)+'  '+path.name+'\n' for path in assets),encoding='utf-8')
    return [*assets,checksums]


def main():
    root=Path(__file__).resolve().parents[1];tag=os.environ.get('INPUT_TAG','')
    release=config(root,tag);signed=os.environ.get('FORKRAIL_SIGNED')=='1'
    if '-' not in release['version'] and not signed:raise ValueError('Stable drafts require signed installers')
    if not tag:raise ValueError('An existing version tag is required')
    commit=verify_tag_commit(tag)
    display_name=release.get('display_name',release['name'])
    body=(display_name+' '+release['version']+'\n\n'
          +'Windows x64 / macOS Apple Silicon / macOS Intel\n\n'
          +('Signed installers; macOS notarization completed.\n' if signed else
            'Unsigned test candidate. macOS Gatekeeper and Windows SmartScreen may block installation. This is not a signed production release.\n')
          +'\nChecksums and platform smoke evidence are attached. Local data and credentials are not included.\n')
    with tempfile.TemporaryDirectory() as temporary:
        destination=Path(temporary)
        notes=destination/'notes.md';notes.write_text(body,encoding='utf-8')
        artifacts=[]
        for target in release['platforms']:
            folder=Path(sys.argv[1])/(display_name+'-'+target)
            artifacts.extend(str(path) for path in prepare_assets(folder,target,destination,signed,commit))
        # Check again after all downloads are validated, immediately before creation.
        verify_tag_commit(tag)
        subprocess.run(['gh','release','create',tag,'--verify-tag','--draft','--title',display_name+' '+release['version'],
                        '--notes-file',str(notes),*(['--prerelease'] if '-' in release['version'] else []),*artifacts],check=True)


if __name__=='__main__':main()
