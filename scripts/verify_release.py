"""Verify an artifact directory before upload or publication."""
import argparse
import json
from pathlib import Path
import sys
from release_tools import config, verify_checksums


def verify(directory, require_signed=False, *, expected_target=None, require_smoke=False, expected_commit=None):
    directory=Path(directory);names=verify_checksums(directory)
    manifest=json.loads((directory/'release-manifest.json').read_text(encoding='utf-8'))
    release=config(Path(__file__).resolve().parents[1])
    if manifest.get('version')!=release['version'] or manifest.get('target') not in release['platforms']:
        raise ValueError('Artifact version or platform mismatch')
    if expected_target and manifest['target']!=expected_target:raise ValueError('Artifact is from a different platform')
    if expected_commit and (manifest.get('source_commit')!=expected_commit or manifest.get('source_dirty') is not False):
        raise ValueError('Artifacts must be built from the exact clean release commit')
    if require_signed and manifest.get('signed') is not True:raise ValueError('Signed artifacts required')
    expected=manifest.get('artifacts',[])
    if len(expected)!=2 or names!=set(expected)|{'release-manifest.json'}:raise ValueError('Incomplete artifact set')
    prefix='Forkrail-'+release['version']+'-'+manifest['target']
    suffixes=['.pkg','.zip'] if manifest['target'].startswith('macos') else ['-Setup.exe','.zip']
    if sorted(expected)!=sorted(prefix+s for s in suffixes):raise ValueError('Unexpected artifact names')
    if require_smoke:
        path=directory/'smoke-result.json'
        if not path.is_file():raise ValueError('Packaged service, UI, and installer evidence is required')
        evidence=json.loads(path.read_text(encoding='utf-8'))
        if evidence.get('target')!=manifest['target'] or evidence.get('version')!=manifest['version']:
            raise ValueError('Smoke evidence version or platform mismatch')
        if evidence.get('user_data_preserved') is not True:raise ValueError('User data preservation was not verified')
        if 'disposable runner' not in evidence.get('installer',''):raise ValueError('The installer was not exercised on a disposable runner')
        groups=['app','portable'] if manifest['target'].startswith('macos') else ['portable','installed']
        for group in groups:
            for mode in ['service','ui']:
                result=evidence.get(group,{}).get(mode,{})
                if result.get('ok') is not True or result.get('mode')!=mode or result.get('version')!=manifest['version']:
                    raise ValueError('Packaged '+group+' '+mode+' smoke did not pass')
    print('Verified '+manifest['target']+' '+manifest['version']+(' (signed)' if manifest['signed'] else ' (unsigned candidate)'))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory');parser.add_argument('--require-signed',action='store_true')
    parser.add_argument('--target');parser.add_argument('--require-smoke',action='store_true');parser.add_argument('--commit')
    args=parser.parse_args()
    try:verify(args.directory,args.require_signed,expected_target=args.target,require_smoke=args.require_smoke,expected_commit=args.commit)
    except (ValueError,OSError,KeyError) as error:print(str(error),file=sys.stderr);sys.exit(1)
