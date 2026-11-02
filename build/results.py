#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Recompute public replay metrics and check summaries against implementation reports."""
import argparse
import csv
import importlib.util
import json
import math
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pipeline_metrics',ROOT/'skill/metrics.py')
metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)

def reproduce():
    summary=json.loads((ROOT/'data/results.json').read_text())
    with (ROOT/'data/replay.csv').open() as stream:
        rows=[{'id':(x['epoch'],x['id']),'source_ns':int(x['source_ns']),'done_ns':int(x['done_ns'])}
              for x in csv.DictReader(stream)]
    start,end=summary['replay']['hot_window_s']
    replay=metrics.measure(rows,0,start,end)
    fields={'fresh_outputs':'hot_true_pose_pair_confirmations','fresh_outputs_per_s':'mean_true_pose_pairs_fps',
            'p95_source_age_ms':'p95_source_age_ms','max_confirmation_gap_ms':'maximum_confirmation_gap_ms'}
    checks=[{'field':field,'passed':math.isclose(replay[field],summary['replay'][key],rel_tol=0,abs_tol=1e-9)}
            for field,key in fields.items()]
    util=(ROOT/'build/fpga/reports/utilization.rpt').read_text()
    resources={}
    for field,label in {'LUT':'CLB LUTs','FF':'CLB Registers','CLB':'CLB','BRAM36_equivalents':'Block RAM Tile','DSP':'DSPs','URAM':'URAM'}.items():
        match=re.search(r'^\|\s*'+re.escape(label)+r'\s*\|\s*(\d+)\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*(\d+)\s*\|',util,re.M)
        if match is None:raise ValueError('Missing utilization row: '+label)
        used,available=map(int,match.groups())
        resources[field]={'used':used,'available':available,'percent':round(100*used/available,2)}
        checks.append({'field':field,'passed':used==summary['hardware_resources'][field]})
    timing=(ROOT/'build/fpga/reports/timing.rpt').read_text().split('| Design Timing Summary',1)[1]
    line=re.search(r'^\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+\d+\s+\d+\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+\d+\s+\d+',timing,re.M)
    if line is None:raise ValueError('Missing design timing summary')
    margins=dict(zip(['setup_ns','TNS_ns','hold_ns','THS_ns'],map(float,line.groups())))
    checks.extend({'field':field,'passed':math.isclose(value,summary['hardware_resources'][field],rel_tol=0,abs_tol=1e-9)} for field,value in margins.items())
    comparison=json.loads((ROOT/'data/comparison.json').read_text())
    before,after=comparison['baseline'],comparison['variant']
    delta={field:after[field]-before[field] for field in ['mean_pairs_fps','p95_source_age_ms','max_gap_ms']}
    delta['throughput_percent']=100*(after['mean_pairs_fps']/before['mean_pairs_fps']-1)
    return {'passed':all(x['passed'] for x in checks),'checks':checks,'replay':replay,'resources':resources,'timing':margins,
            'comparison_delta':delta,'scope':'Recomputed saved public data; no new inference, implementation or board run. DDR values are saved summaries, not independently recomputed traces.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=reproduce();text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.write_text(text)
    print(text,end='');raise SystemExit(0 if result['passed'] else 1)
