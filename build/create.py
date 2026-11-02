#!/usr/bin/env python3
"""Create the di46 project from collected HDL, BD Tcl and the original DPU IP."""
import argparse
from pathlib import Path
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True,help='Fresh work directory')
    p.add_argument('--vivado',type=Path,required=True,help='Explicit Vivado 2026.1 executable')
    p.add_argument('--dpu-repo',type=Path,help='Existing DPU IP repository (VAI v3.0)')
    p.add_argument('--generate',action='store_true',help='Also generate BD/IP output products')
    a=p.parse_args();work=a.output.resolve();work.mkdir(parents=True,exist_ok=False)
    if a.dpu_repo:
        ip=a.dpu_repo.resolve()
    else:
        with tarfile.open(ROOT/'build/vendor.tar.gz') as archive:
            archive.extractall(work/'vendor',filter='data')
        ip=work/'vendor/dpu_ip_v3/DPUCZDX8G_ip_repo_VAI_v3.0'
    command=[str(a.vivado.resolve()),'-mode','batch','-source',str(ROOT/'build/create.tcl'),
             '-log',str(work/'vivado.log'),'-journal',str(work/'vivado.jou'),'-tclargs',
             str(work/'project'),str(ip),'generate' if a.generate else 'validate']
    with (work/'console.log').open('w') as log:
        run=subprocess.run(command,cwd=work,stdout=log,stderr=subprocess.STDOUT)
    print(f'Vivado exit={run.returncode}; log={work / "console.log"}')
    raise SystemExit(run.returncode)
