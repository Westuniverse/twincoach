#!/usr/bin/env python3
"""Restore the complete development layout and overlay the current reviewable sources."""
import argparse
import json
from pathlib import Path
import shutil
import tarfile

ROOT=Path(__file__).resolve().parents[1]
def restore(output, vendor=False):
    output.mkdir(parents=True,exist_ok=False)
    with tarfile.open(ROOT/'src/dev.tar.gz') as archive:
        archive.extractall(output,filter='data')
    def tree(source,target):
        shutil.copytree(source,target,dirs_exist_ok=True)
    tree(ROOT/'src/host/posepriv_coach',output/'src/posepriv_coach')
    for item in json.loads((ROOT/'build/native.json').read_text()):
        target=output/'board_runtime/full33'/item['runtime']
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/item['path'],target)
    for source in (ROOT/'src/drivers').rglob('*'):
        if source.is_file():
            relative=source.relative_to(ROOT/'src/drivers')
            if relative.name=='makefile':relative=relative.with_name('Makefile')
            elif relative.name=='readme.md':relative=relative.with_name('README.md')
            target=output/'board_drivers'/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
    tree(ROOT/'src/software',output/'software')
    for item in json.loads((ROOT/'build/di46/sources.json').read_text())['files']:
        target=output/item['source'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/item['destination'],target)
    if vendor:
        with tarfile.open(ROOT/'build/vendor.tar.gz') as archive:
            archive.extractall(output/'tmp',filter='data')
    return output
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--vendor',action='store_true')
    args=parser.parse_args()
    print(restore(args.output.resolve(),args.vendor))
