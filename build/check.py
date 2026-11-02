#!/usr/bin/env python3
"""Check the public source layout and run host-only functional tests."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
checks=[]
def record(name,passed,**details):checks.append(dict(check=name,passed=bool(passed),**details))
expected={'src','sim','build','board','data','skill','report'}
record('guide_layout',{p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith('.')}==expected)
records=json.loads((ROOT/'build/sources.json').read_text())
record('runtime_source_closure',all((ROOT/x['path']).is_file() for x in records),files=len(records))
for x in records:
 path=ROOT/x['path']
 if path.suffix=='.py':ast.parse(path.read_text(),filename=x['path'])
record('python_source_parse',True)
hdl=json.loads((ROOT/'build/fpga/sources.json').read_text())['files']
record('actual_hdl_inputs',len(hdl)==44 and all((ROOT/x['destination']).is_file() for x in hdl),files=len(hdl))
for path in ROOT.glob('board/*.ps1'):
 for name in re.findall(r'([a-zA-Z0-9_-]+\.ps1)',path.read_text()):
  record('control_reference',(path.parent/name).is_file(),script=path.name,target=name)
# Current public documents use explicit links; historical records remain separately labelled.
documents=[ROOT/name for name in ['readme.md','build/readme.md','report/design.md','report/results.md','report/assets.md','report/third_party.md','report/development.md','report/collaboration.md']]
documents.extend((ROOT/'skill').rglob('*.md'))
for path in documents:
 name=str(path.relative_to(ROOT))
 path=ROOT/name
 for target in re.findall(r'\]\(([^)]+)\)',path.read_text()):
  if '://' not in target and not target.startswith('#'):
   record('document_link',(path.parent/target.split('#')[0]).exists(),document=name,target=target)
run=subprocess.run([sys.executable,'-m','unittest','discover','-s','sim','-p','test_*.py','-v'],
 cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
(ROOT/'sim/tests.log').write_text(run.stdout+run.stderr)
record('host_tests',run.returncode==0,exit_code=run.returncode)
result={'project':'TwinCoach','passed':all(x['passed'] for x in checks),'checks':checks,
 'scope':'Public sources and host functions; does not execute model inference, synthesis or board operation.'}
(ROOT/'sim/public.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':result['passed'],'checks':len(checks),'failures':[x for x in checks if not x['passed']]},ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
