#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Inspect or verify a contract-defined PYNQ stream transaction."""
import argparse
import csv
import json
from pathlib import Path
import time
from contracts import load
from backends import MockBackend,PynqBackend

def execute(contract, vectors, size, backend):
    started=time.monotonic_ns()
    actual=backend.transfer(vectors['input'],size)
    finished=time.monotonic_ns()
    differences=[{'index':i,'expected':want,'actual':got}
                 for i,(want,got) in enumerate(zip(vectors['expected'],actual)) if want!=got]
    passed=len(actual)==len(vectors['expected']) and not differences
    return {'schema':'stream-result-v1','case':contract['name'],'backend':backend.name,
            'hardware_executed':backend.hardware,'passed':passed,
            'dtype':contract['dtype'],'elements':contract['count'],'bytes':size,
            'expected':vectors['expected'],'actual':actual,'differences':differences,
            'actual_elements':len(actual),'length_matches':len(actual)==len(vectors['expected']),
            'events':list(backend.events),'wall_ns':finished-started,
            'measurement_scope':'CPU software model' if not backend.hardware else 'Allocate, configure, DMA completion and release; includes host overhead',
            'board_performance_verified':False,'source_ns':started,'done_ns':finished}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['inspect','run'])
    p.add_argument('--contract',type=Path,required=True)
    p.add_argument('--vectors',type=Path,required=True)
    p.add_argument('--backend',choices=['mock','pynq'],default='mock')
    p.add_argument('--bitstream',type=Path)
    p.add_argument('--program',action='store_true',help='Explicitly download this overlay before access')
    p.add_argument('--output',type=Path)
    p.add_argument('--trace',type=Path,help='Append one completed sample to a metrics CSV')
    p.add_argument('--sample-id',help='Unique source identity for an appended trace row')
    args=p.parse_args()
    if args.trace and (not args.sample_id or args.action!='run'):p.error('--trace requires run and a unique --sample-id')
    if args.program and (args.backend!='pynq' or args.action!='run'):
        p.error('--program is only available for an explicit PYNQ run')
    if args.backend=='pynq' and args.bitstream is None:p.error('PYNQ requires --bitstream')
    backend=None
    try:
        contract,vectors,size=load(args.contract,args.vectors)
        backend=MockBackend(contract) if args.backend=='mock' else PynqBackend(contract,args.bitstream,program=args.program)
        result=backend.inspect() if args.action=='inspect' else execute(contract,vectors,size,backend)
    except Exception as error:
        events=list(backend.events) if backend is not None else []
        result={'schema':'stream-result-v1','case':args.contract.stem,'backend':args.backend,
                'hardware_executed':args.backend=='pynq' and 'receive_submit' in events,
                'passed':False,'error_type':type(error).__name__,'error':str(error),'events':events,
                'buffers_retained':len(getattr(backend,'buffers',[])) if backend is not None else 0,
                'board_performance_verified':False}

    text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.write_text(text)
    if args.trace and result['passed']:
        header=not args.trace.exists() or args.trace.stat().st_size==0
        with args.trace.open('a',newline='') as stream:
            writer=csv.writer(stream)
            if header:writer.writerow(['id','source_ns','done_ns'])
            writer.writerow([args.sample_id,result['source_ns'],result['done_ns']])
    print(text,end='')
    return 0 if result.get('passed',True) else 1

if __name__=='__main__':raise SystemExit(main())
