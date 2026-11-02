#!/usr/bin/env python3
"""Repack the final Full33 runtime with the primary reviewed source files."""
import argparse
import io
import json
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parents[1]
def overlays():
    result={}
    for source in (ROOT/'src/host').rglob('*.py'):
        result['src/'+str(source.relative_to(ROOT/'src/host'))]=source
    for item in json.loads((ROOT/'build/native.json').read_text()):
        result[item['runtime']]=ROOT/item['path']
    for source in (ROOT/'src/native/common').iterdir():
        if source.is_file():result['board_tests/'+source.name]=source
    for source in (ROOT/'src/drivers').rglob('*'):
        if not source.is_file():continue
        rel=source.relative_to(ROOT/'src/drivers')
        if rel.name=='makefile':rel=rel.with_name('Makefile')
        elif rel.name=='readme.md':rel=rel.with_name('README.md')
        result['board_drivers/'+str(rel)]=source
    for source in (ROOT/'data/config').rglob('*'):
        if source.is_file():result['config/'+str(source.relative_to(ROOT/'data/config'))]=source
    return result

def pack(output):
    source_map=overlays();replaced=[];changed=[]
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream, tarfile.open(fileobj=stream,mode='w:gz',compresslevel=4) as result:
        with tarfile.open(ROOT/'board/runtime.tar.gz') as baseline:
            for member in baseline:
                name=member.name.removeprefix('./')
                if member.isfile() and name in source_map:
                    data=source_map[name].read_bytes()
                    old=baseline.extractfile(member).read()
                    if data!=old:changed.append(name)
                    member.size=len(data)
                    result.addfile(member,io.BytesIO(data));replaced.append(name)
                else:
                    result.addfile(member,baseline.extractfile(member) if member.isfile() else None)
        info=json.loads((ROOT/'board/version.json').read_text())
        info={'project':'PosePriv-Coach','variant':'dual_full33_no_video','base':info,
              'source_overlays':len(replaced),'changed_from_selected_runtime':changed,
              'scope':'Repack of the selected runtime and reviewed source; no new native compilation or board run.'}
        data=(json.dumps(info,ensure_ascii=False,indent=2)+'\n').encode()
        member=tarfile.TarInfo('release.json');member.size=len(data);member.mode=0o644
        result.addfile(member,io.BytesIO(data))
    return info
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    info=pack(args.output.resolve())
    print(json.dumps({'archive':str(args.output),'overlays':info['source_overlays'],
                     'changed':info['changed_from_selected_runtime']},ensure_ascii=False))
