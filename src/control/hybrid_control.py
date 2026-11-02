#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Explicit hybrid cold selector over the original controller/state/supervisor."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import control


expand_package=control.expand_package


def main():
    control.ENTRY=ROOT/'native/hybrid/stream_profile/camera_entry.py'
    argv=list(sys.argv)
    if len(argv)>1 and argv[1]=='start':
        control.STATE.mkdir(exist_ok=True)
        template=json.loads((ROOT/'hybrid_lightning_config.template.json').read_text())
        control.write(control.STATE/'hybrid-lightning.json',expand_package(template))
        overrides=json.loads((ROOT/'hybrid_overrides.example.json').read_text())
        if '--config-overrides'in argv:
            index=argv.index('--config-overrides')+1
            supplied=json.loads(Path(argv[index]).read_text())
            equals_form=False
        elif any(v.startswith('--config-overrides=')for v in argv):
            index=next(i for i,v in enumerate(argv)if v.startswith('--config-overrides='))
            supplied=json.loads(Path(argv[index].split('=',1)[1]).read_text())
            equals_form=True
        else:
            supplied=None;equals_form=False
            argv+=['--config-overrides',''];index=len(argv)-1
        if supplied is not None:
            overrides['config'].update(expand_package(supplied['config']))
            overrides['package_relative_fields']=list(dict.fromkeys(
                overrides.get('package_relative_fields',[])+supplied.get('package_relative_fields',[])))
        path=control.STATE/'hybrid-overrides.json';control.write(path,overrides)
        argv[index]=('--config-overrides='if equals_form else'')+str(path)
    sys.argv=argv
    control.main()


if __name__=='__main__':main()
