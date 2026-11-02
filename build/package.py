#!/usr/bin/env python3
"""Package the local PosePriv-Coach submission materials under the guide layout."""
import argparse
import json
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();output=args.output.resolve()
    if output.is_relative_to(ROOT):raise ValueError('Output must be outside the source directory')
    output.parent.mkdir(parents=True,exist_ok=True)
    entries=[]
    with tarfile.open(output,'x:gz',compresslevel=4) as archive:
        for path in sorted(ROOT.rglob('*')):
            if not path.is_file():continue
            relative=path.relative_to(ROOT)
            if any(x in {'.git','__pycache__','.pytest_cache'} for x in relative.parts):continue
            archive.add(path,arcname=str(Path('twincoach')/relative),recursive=False)
            entries.append({'path':str(relative),'bytes':path.stat().st_size})
    summary={'project':'TwinCoach','version':'2026-11-02 dual Full33 no video',
             'archive':output.name,'archive_bytes':output.stat().st_size,'files':len(entries),
             'layout':['src','sim','build','board','data','skill','report'],
             'scope':'Local competition materials; existing native/firmware assets retained. No new Vivado or board qualification.',
             'open_items':'report/gaps.md','inventory':entries}
    output.with_name(output.name.removesuffix('.tar.gz')+'.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='inventory'},ensure_ascii=False))
