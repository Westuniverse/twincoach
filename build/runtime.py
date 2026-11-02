#!/usr/bin/env python3
"""Build a relocatable runtime archive from sources and an explicit asset manifest."""
import argparse
from pathlib import Path
import tarfile
import tempfile
from stage import stage

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--assets',type=Path,help='Optional model, library and firmware asset manifest')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='twincoach-runtime-') as temporary:
        folder=Path(temporary)/'runtime'
        info=stage(folder,args.assets.resolve() if args.assets else None)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with tarfile.open(args.output,'x:gz') as archive:
            for path in sorted(folder.rglob('*')):
                if path.is_file():archive.add(path,arcname=str(path.relative_to(folder)),recursive=False)
    print(f'Runtime archive: {args.output}; missing assets: {len(info["missing_required_assets"])}')
