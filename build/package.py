#!/usr/bin/env python3
"""Package the tracked TwinCoach project sources."""
import argparse
from pathlib import Path
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();output=args.output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    paths=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    with tarfile.open(output,'x:gz') as archive:
        for name in filter(None,paths):
            path=ROOT/name
            if path.is_file():archive.add(path,arcname=str(Path('twincoach')/name),recursive=False)
    print(output)
