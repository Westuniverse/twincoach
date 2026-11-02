#!/usr/bin/env python3
"""Control a TwinCoach board over SSH from Windows, Linux or macOS."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
REMOTE=r'''
import importlib.util,json,os,pathlib,platform,subprocess,sys
cfg=json.load(sys.stdin)
root=pathlib.Path(cfg['root']).expanduser()
action=cfg['action']
if action=='check':
    paths=['control.py','runtime_config.template.json','native/profile/entry/run_preview.py']
    checks={p:(root/p).is_file() for p in paths}
    checks.update({name:importlib.util.find_spec(name) is not None for name in ('numpy','cv2','PIL')})
    checks['architecture']=platform.machine()=='aarch64'
    checks['camera_front']=pathlib.Path(cfg['front']).exists()
    checks['camera_side']=pathlib.Path(cfg['side']).exists()
    for device in ('/dev/posepriv_tail4','/dev/posepriv_shared_pose_dma','/dev/dri/renderD128'):
        checks[device]=pathlib.Path(device).exists()
    print(json.dumps({'root':str(root),'checks':checks},ensure_ascii=False,indent=2))
    sys.exit(0 if all(checks.values()) else 1)
entry='hybrid_control.py' if cfg['mode']=='hybrid' and action=='start' else 'control.py'
command=['python3',str(root/entry),'rollback' if action=='restore' else action]
if action=='start':
    prepare=subprocess.run(['bash',str(root/'scripts/board/prepare_tail4_device.sh'),str(root)])
    if prepare.returncode:sys.exit(prepare.returncode)
    authority=cfg['xauthority'] or os.environ.get('XAUTHORITY') or '/run/user/'+str(os.getuid())+'/gdm/Xauthority'
    mapping={'front':'side','side':'front'} if cfg['swap_views'] else {'front':'front','side':'side'}
    profile=pathlib.Path(cfg['profile']).expanduser()
    if not profile.is_absolute():profile=root/profile
    command+=['--front',cfg['front'],'--side',cfg['side'],
        '--front-rotation',str(cfg['front_rotation']),'--side-rotation',str(cfg['side_rotation']),
        '--xauthority',authority,'--display',cfg['display'],'--scorer-mapping',json.dumps(mapping),'--config-overrides',str(profile)]
elif action=='restore':command+=['--p4-command-file',str(pathlib.Path(cfg['command_file']).expanduser())]
sys.exit(subprocess.run(command,cwd=root).returncode)
'''

def settings(args):
    cfg={'host':os.environ.get('TWINCOACH_HOST',''),'user':os.environ.get('TWINCOACH_USER','ubuntu'),
         'root':os.environ.get('TWINCOACH_ROOT','~/twincoach'),'port':22,'front':'/dev/video0','side':'/dev/video2',
         'front_rotation':90,'side_rotation':270,'xauthority':'','display':':0','mode':'full33',
         'profile':'demo-overrides.json','swap_views':False,'command_file':''}
    if args.config:cfg.update(json.loads(args.config.read_text()))
    for name in cfg:
        value=getattr(args,name,None)
        if value is not None:cfg[name]=value
    cfg['action']=args.action
    return cfg

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','status','check','restore'])
    parser.add_argument('--config',type=Path)
    for name in ('host','user','root','front','side','xauthority','display','profile','command-file'):
        parser.add_argument('--'+name)
    parser.add_argument('--port',type=int)
    parser.add_argument('--front-rotation',type=int,choices=[0,90,180,270])
    parser.add_argument('--side-rotation',type=int,choices=[0,90,180,270])
    parser.add_argument('--mode',choices=['full33','hybrid'])
    parser.add_argument('--swap-views',action='store_true',default=None)
    parser.add_argument('--dry-run',action='store_true',help='Print the SSH plan without connecting')
    parser.add_argument('--local',action='store_true',help='Check this source checkout without connecting')
    args=parser.parse_args();cfg=settings(args)
    if args.local:
        paths=['src/control/control.py','data/examples/runtime.json','build/sources.json'] if (ROOT/'build/sources.json').is_file() else ['control.py','runtime_config.template.json','native/profile/entry/run_preview.py']
        checks={p:(ROOT/p).is_file() for p in paths}
        checks['ssh']=shutil.which('ssh') is not None
        print(json.dumps({'checks':checks},indent=2));return 0 if all(checks.values()) else 1
    if not cfg['host']:parser.error('Set --host, TWINCOACH_HOST or host in --config')
    if args.action=='restore' and not cfg['command_file']:parser.error('restore requires --command-file')
    target=cfg['host'] if '@' in cfg['host'] else cfg['user']+'@'+cfg['host']
    command=['ssh','-p',str(cfg['port']),'-o','ConnectTimeout=8','-o','ServerAliveInterval=5',
             '-o','ServerAliveCountMax=3',target,'python3 -c '+shlex.quote(REMOTE)]
    if args.dry_run:
        print(json.dumps({'target':target,'port':cfg['port'],'settings':cfg},ensure_ascii=False,indent=2));return 0
    return subprocess.run(command,input=json.dumps(cfg),text=True).returncode

if __name__=='__main__':raise SystemExit(main())
