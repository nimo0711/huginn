"""Generate a user LaunchAgent file without installing or starting a service."""
import argparse
from pathlib import Path
import plistlib
import sys


def configuration(repo, python):
    return {
        'Label':'local.work-board',
        'ProgramArguments':[str(Path(python).resolve()),str(repo/'server.py')],
        'WorkingDirectory':str(repo),
        'RunAtLoad':True,
        'KeepAlive':True,
        'StandardErrorPath':str(repo/'server.err.log'),
    }


def main():
    repo=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=repo/'build/local.work-board.plist')
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(plistlib.dumps(configuration(repo,sys.executable)))
    print(args.output)


if __name__=='__main__':main()
