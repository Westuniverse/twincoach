#!/usr/bin/env python3
"""Measure fresh-output rate, source age and confirmation gap in a fixed window."""
import argparse
import csv
import json
import math
from pathlib import Path

def measure(rows, origin_ns, start_s, end_s):
    begin=origin_ns+round(start_s*1e9);end=origin_ns+round(end_s*1e9)
    seen=set();selected=[]
    for row in sorted(rows,key=lambda x:x['done_ns']):
        if row['id'] in seen:continue
        seen.add(row['id'])
        if begin<=row['done_ns']<end:selected.append(row)
    if not selected:raise ValueError('No fresh confirmations in the selected window')
    ages=sorted((x['done_ns']-x['source_ns'])/1e6 for x in selected)
    gaps=[(b['done_ns']-a['done_ns'])/1e6 for a,b in zip(selected,selected[1:])]
    return {'window_s':[start_s,end_s],'fresh_outputs':len(selected),
            'fresh_outputs_per_s':len(selected)/(end_s-start_s),
            'p95_source_age_ms':ages[math.floor(.95*(len(ages)-1))],
            'max_confirmation_gap_ms':max(gaps,default=0),
            'p95_method':'sorted[floor(.95*(n-1))]'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path)
    p.add_argument('--start',type=float,default=15)
    p.add_argument('--end',type=float,default=44)
    p.add_argument('--origin-ns',type=int,default=0,help='CSV timestamp origin; JSON uses replay_start_ns')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.input.suffix=='.csv':
        with a.input.open() as stream:
            rows=[{'id':x['id'],'source_ns':int(x['source_ns']),'done_ns':int(x['done_ns'])} for x in csv.DictReader(stream)]
        origin=a.origin_ns
    else:
        raw=json.loads(a.input.read_text());origin=raw['replay_start_ns']
        rows=[{'id':tuple(x['source_ids']),'source_ns':x['source_timestamp_ns'],'done_ns':x['completed_ns']}
              for x in raw['final']['presentation_samples'] if x.get('fresh_pose_pair')]
    result=measure(rows,origin,a.start,a.end)
    text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if a.output:a.output.write_text(text)
    print(text,end='')
