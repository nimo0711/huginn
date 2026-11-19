"""Fail before expensive CI jobs on an invalid version/tag/signing request."""
import os
from pathlib import Path
import subprocess
from release_tools import config


def verify_tag_commit(tag):
    refs=subprocess.check_output(['git','ls-remote','--exit-code','origin','refs/tags/'+tag,'refs/tags/'+tag+'^{}'],text=True).splitlines()
    matches={name:sha for sha,name in (line.split() for line in refs)}
    commit=matches.get('refs/tags/'+tag+'^{}') or matches.get('refs/tags/'+tag)
    head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    if commit!=head:raise ValueError('Run the workflow at the exact existing tag commit')
    return head


def main():
    tag=os.environ.get('INPUT_TAG') or (os.environ.get('GITHUB_REF_NAME') if os.environ.get('GITHUB_REF_TYPE')=='tag' else None)
    release=config(Path(__file__).resolve().parents[1],tag)
    if os.environ.get('PUBLISH_DRAFT')=='1':
        if not tag:raise ValueError('A pre-existing version tag is required for a draft')
        if '-' not in release['version'] and os.environ.get('HUGINN_SIGNED')!='1':
            raise ValueError('Stable release drafts require signed installers')
        verify_tag_commit(tag)
    print(release['version'])


if __name__=='__main__':main()
