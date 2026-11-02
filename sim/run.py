#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run the declared Verilator self-checks and retain commands, logs and verdicts."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def run(output, executable, timeout):
    tool=shutil.which(executable)
    if tool is None:raise SystemExit('Verilator not found: '+executable)
    output.mkdir(parents=True,exist_ok=False)
    version=subprocess.check_output([tool,'--version'],text=True).strip()
    results=[]
    for case in json.loads((ROOT/'sim/rtl.json').read_text()):
        if not case['enabled'] or not case['simulate']:continue
        folder=output/case['name'];folder.mkdir()
        command=[tool,'--binary','--assert',*case['flags'],'--top-module',case['top'],
                 '--Mdir',str(folder/'obj'),'-o','testbench',
                 *['-I'+str(ROOT/path) for path in case['include_dirs']],
                 *[str(ROOT/path) for path in case['sources']]]
        record={'name':case['name'],'top':case['top'],'sources':case['sources'],'compile_command':command,
                'run_command':[str(folder/'obj/testbench')],'passed':False}
        try:
            with (folder/'compile.log').open('w') as log:
                compile_run=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
            record['compile_exit_code']=compile_run.returncode
            if compile_run.returncode:
                record['failure']='compile';results.append(record);continue
            with (folder/'run.log').open('w') as log:
                simulation=subprocess.run(record['run_command'],cwd=folder,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
            text=(folder/'run.log').read_text()
            marker='TB PASS: '+case['name']
            record.update(run_exit_code=simulation.returncode,pass_marker=marker,
                          passed=simulation.returncode==0 and marker in text and not re.search(r'%Error|Assertion failed|Fatal:',text))
            if not record['passed']:record['failure']='simulation'
        except subprocess.TimeoutExpired:
            record['failure']='timeout'
        results.append(record)
    result={'backend':'Verilator','tool':version,'mode':'behavioral','passed':bool(results) and all(x['passed'] for x in results),
            'cases':results,'scope':'Three declared arithmetic/handshake testbenches; not full-network, post-route or board verification.'}
    (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='Fresh output directory')
    parser.add_argument('--verilator',default='verilator')
    parser.add_argument('--timeout',type=float,default=120,help='Seconds per compilation or simulation')
    args=parser.parse_args();result=run(args.output.resolve(),args.verilator,args.timeout)
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),'results':str(args.output/'results.json')}))
    raise SystemExit(0 if result['passed'] else 1)
