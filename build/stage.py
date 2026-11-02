#!/usr/bin/env python3
"""Stage the public runtime sources, optionally adding explicitly supplied assets."""
import argparse
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
def stage(output, manifest=None, *, mode="full33"):
    output.mkdir(parents=True,exist_ok=False)
    copied=[]
    for item in json.loads((ROOT/'build/sources.json').read_text()):
        destination=output/item['runtime'];destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/item['path'],destination);copied.append(item['runtime'])
    contract=json.loads((ROOT/'build/assets.json').read_text())
    supplied={} if manifest is None else json.loads(manifest.read_text()).get('artifacts',{})
    missing=[];included=[]
    for asset in contract['artifacts']:
        if mode not in asset.get('modes',['full33','hybrid']):continue
        item=supplied.get(asset['role'])
        if item is None:
            if asset['required']:missing.append({'role':asset['role'],'destination':asset['destination']})
            continue
        if not item.get('origin') or not item.get('license'):
            raise ValueError('Asset origin and license required: '+asset['role'])
        source=Path(item['source']).expanduser()
        if not source.is_absolute():source=manifest.parent/source
        target=output/asset['destination'];target.parent.mkdir(parents=True,exist_ok=True)
        if source.is_dir():shutil.copytree(source,target,dirs_exist_ok=True)
        else:shutil.copy2(source,target)
        included.append(asset['role'])
    report={'project':'TwinCoach','mode':mode,'sources':len(copied),'included_assets':included,
            'missing_required_assets':missing,'ready_for_board':not missing,
            'scope':'Source staging and explicit assets; no native compilation or board qualification.'}
    (output/'staging.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--assets',type=Path)
    parser.add_argument('--mode',choices=['full33','hybrid'],default='full33')
    args=parser.parse_args();r=stage(args.output.resolve(),args.assets.resolve() if args.assets else None,mode=args.mode)
    print(json.dumps({'sources':r['sources'],'missing_required_assets':len(r['missing_required_assets']),
                      'ready_for_board':r['ready_for_board']},ensure_ascii=False))
