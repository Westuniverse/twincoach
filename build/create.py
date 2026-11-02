#!/usr/bin/env python3
"""Create the TwinCoach FPGA project using an explicit platform configuration."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True,help='Fresh work directory')
    p.add_argument('--vivado',type=Path,required=True,help='Vivado executable')
    p.add_argument('--dpu-repo',type=Path,required=True,help='DPU IP repository')
    p.add_argument('--platform',type=Path,default=ROOT/'data/platform.json')
    p.add_argument('--generate',action='store_true',help='Also generate BD/IP output products')
    a=p.parse_args();work=a.output.resolve();work.mkdir(parents=True,exist_ok=False)
    ip=a.dpu_repo.resolve()
    platform=json.loads(a.platform.read_text())
    command=[str(a.vivado.resolve()),'-mode','batch','-source',str(ROOT/'build/create.tcl'),
             '-log',str(work/'vivado.log'),'-journal',str(work/'vivado.jou'),'-tclargs',
             str(work/'project'),str(ip),'generate' if a.generate else 'validate',
             platform['part'],platform['board_part'],platform['vivado']]
    with (work/'console.log').open('w') as log:
        run=subprocess.run(command,cwd=work,stdout=log,stderr=subprocess.STDOUT)
    print(f'Vivado exit={run.returncode}; log={work / "console.log"}')
    raise SystemExit(run.returncode)
