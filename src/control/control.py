#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Cold board-local configuration/control; no inference runs on the laptop."""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'state'
SESSION=STATE/'session.json'
ENTRY=ROOT/'native/profile/entry/run_preview.py'
PATH_KEYS=('python_path','worker','model','xclbin','skinning_validator','mesh','rules',
    'preview_affine_library','preview_bone_affine_library','preview_normal_prep_library',
    'preview_rig_tail_library','tail4_detector_model','tail4_crop_library','tail4_roi_crop_library','tail4_decode_library',
    'tail4_memfd_module_dir','preview_native_gl_submit_library','preview_jpeg_library')


def expand_package(value):
    """Resolve the same relocatable profile paths for Full33 and Hybrid."""
    if isinstance(value,dict):return {k:expand_package(v)for k,v in value.items()}
    if isinstance(value,list):return [expand_package(v)for v in value]
    if value=='PACKAGE_ROOT':return str(ROOT)
    if isinstance(value,str)and value.startswith('PACKAGE_ROOT/'):
        return str(ROOT/value[len('PACKAGE_ROOT/'):])
    return value


def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def current(session):
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=session['boot_id']:return False
    try:
        proc=Path('/proc')/str(session['pid'])
        ticks=int((proc/'stat').read_text().rsplit(') ',1)[1].split()[19])
        argv=[os.fsdecode(v)for v in(proc/'cmdline').read_bytes().removesuffix(b'\0').split(b'\0')]
        return ticks==session['start_ticks']and argv==session['command']and proc.stat().st_uid==os.getuid()
    except FileNotFoundError:return False


def save_child(command,environment,cwd,*,kind='full33',session_file=None):
    STATE.mkdir(exist_ok=True)
    with(STATE/'preview.log').open('ab',buffering=0)as log:
        child=subprocess.Popen(command,cwd=cwd,env=dict(os.environ,**environment),
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    session=dict(kind=kind,pid=child.pid,
        start_ticks=int(Path(f'/proc/{child.pid}/stat').read_text().rsplit(') ',1)[1].split()[19]),
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        command=command,cwd=str(cwd))
    write(SESSION,session)
    if session_file is not None:
        previous=json.loads(session_file.read_text())if session_file.exists()else{}
        write(session_file,dict(previous,**session))
    return session


def stop_record(session):
    try:fd=os.pidfd_open(session['pid'])
    except ProcessLookupError:return dict(status='owned_session_already_ended')
    try:
        # Bind the pidfd first, then check the explicit source record. Never
        # signal a PID reused by another user/boot/command/start timestamp.
        if not current(session):return dict(status='session_not_current')
        signal.pidfd_send_signal(fd,signal.SIGINT)
        poll=select.poll();poll.register(fd,select.POLLIN)
        if not poll.poll(20000):raise TimeoutError('Owned supervisor has not exited after20s; inspect state/preview.log')
    finally:os.close(fd)
    return dict(status='owned_session_stopped',pid=session['pid'])


def stop():
    if not SESSION.exists():return dict(status='no_owned_session')
    return stop_record(json.loads(SESSION.read_text()))


def start_owned(command,environment,cwd,*,previous_session_file=None):
    # The caller has already resolved the command and checked required assets.
    # A recorded package P4 is intentionally handed over; a live Full33 is reused.
    owned=None
    if SESSION.exists():
        owned=json.loads(SESSION.read_text())
        if current(owned):
            if owned.get('kind','full33')=='full33':
                return dict(status='full33_already_running',pid=owned['pid'])
        else:owned=None
    previous=(None if previous_session_file is None else
              json.loads(previous_session_file.read_text()))
    if owned is not None:stop_record(owned)
    if previous is not None and current(previous):stop_record(previous)
    return save_child(command,environment,cwd,kind='full33')


def rollback_owned(old,*,command_file):
    session_file=old.get('session_file')
    if session_file is not None:
        session_file=Path(session_file)
        if not session_file.is_absolute():session_file=command_file.parent/session_file
        if session_file.exists():
            previous=json.loads(session_file.read_text())
            if previous['command']==old['command']and current(previous):
                return dict(status='p4_already_running',pid=previous['pid'])
    if SESSION.exists():
        owned=json.loads(SESSION.read_text())
        if (owned.get('kind')=='p4'and owned['command']==old['command']and current(owned)):
            return dict(status='p4_already_running',pid=owned['pid'])
    stop()
    return save_child(old['command'],old.get('environment',{}),old['cwd'],
        kind='p4',session_file=session_file)


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    start=sub.add_parser('start');start.add_argument('--front',required=True);start.add_argument('--side',required=True)
    start.add_argument('--front-rotation',type=int,default=90);start.add_argument('--side-rotation',type=int,default=270)
    start.add_argument('--scorer-mapping',default='{"front":"front","side":"side"}')
    start.add_argument('--arm-recovery',action='store_true');start.add_argument('--yaw60',action='store_true')
    start.add_argument('--display',default=':0');start.add_argument('--xauthority',required=True)
    start.add_argument('--dry-run',action='store_true');start.add_argument('--seconds',type=float,default=0)
    start.add_argument('--config-overrides',type=Path,help='Explicit cold extra measured config/path fields, e.g. a later JPEG decoder')
    start.add_argument('--previous-session-file',type=Path,
        help='Explicit recorded current P4 session to cooperatively stop after resolving all assets/command')
    sub.add_parser('stop');sub.add_parser('status')
    rollback=sub.add_parser('rollback');rollback.add_argument('--p4-command-file',type=Path,required=True)
    a=p.parse_args()
    if a.action=='stop':print(json.dumps(stop()));return
    if a.action=='status':
        session=None if not SESSION.exists()else json.loads(SESSION.read_text())
        print(json.dumps(dict(session=session,running=session is not None and current(session))));return
    if a.action=='rollback':
        old=json.loads(a.p4_command_file.read_text())
        if not isinstance(old.get('command'),list)or not old['command']or not old.get('cwd'):
            raise ValueError('Fill the exact saved P4 command/cwd/environment first')
        print(json.dumps(rollback_owned(old,command_file=a.p4_command_file)));return
    config=json.loads((ROOT/'runtime_config.template.json').read_text())
    extra_paths=[]
    if a.config_overrides:
        extra=expand_package(json.loads(a.config_overrides.read_text()));config.update(extra['config'])
        extra_paths=extra.get('package_relative_fields',[])
    for key in(*PATH_KEYS,*extra_paths):config[key]=str(ROOT/config[key])
    config['tail4_worker_command']=[str(ROOT/v)if not v.startswith('/dev/')else v for v in config['tail4_worker_command']]
    config['tail4_scoring_view_mapping']=json.loads(a.scorer_mapping)
    config['preview_arm_recovery']=a.arm_recovery
    config['preview_avatar_yaw_degrees']=60 if a.yaw60 else 30
    STATE.mkdir(exist_ok=True);config_path=STATE/'live-config.json';write(config_path,config)
    command=['/bin/bash',str(ROOT/'scripts/board/run_shared_pose_session.sh'),str(ROOT),
        str(ROOT/'runtime/posepriv_shared_dma.ko'),'--',sys.executable,
        str(ROOT/'scripts/board/run_camera_dynamic_fps_session.py'),
        '--front',a.front,'--side',a.side,'--dynamic-framerate','0','--',sys.executable,str(ENTRY),
        '--front',a.front,'--side',a.side,'--front-rotation',str(a.front_rotation),
        '--side-rotation',str(a.side_rotation),'--config',str(config_path),
        '--seconds',str(a.seconds),'--report',str(STATE/'capture-report.json')]
    environment=dict(DISPLAY=a.display,XAUTHORITY=a.xauthority,
        # Tiny pose matrices share the A53s with capture, NN and HDMI owners.
        OPENBLAS_NUM_THREADS='1',
        PYTHONPATH=os.pathsep.join((str(ROOT),str(ROOT/'src'),str(ROOT/'python'))),
        POSEPRIV_COOPERATIVE_PREVIEW_STOP='1',LD_LIBRARY_PATH='/opt/vitis-ai-compat/lib:/lib/aarch64-linux-gnu:/lib',
        XLNX_VART_FIRMWARE=str(ROOT/'runtime/posepriv-dpu-isolation.xclbin'),XLNX_DISABLE_LOAD_XCLBIN='0')
    if a.dry_run:
        print(json.dumps(dict(status='command_only_not_started',command=command,environment=environment),ensure_ascii=False));return
    staged=json.loads((ROOT/'staging_report.json').read_text())
    if not staged['assembled_required_artifacts']:
        raise RuntimeError('Source-only package: collect explicit missing_artifacts.json and rebuild before start')
    print(json.dumps(start_owned(command,environment,ROOT,
        previous_session_file=a.previous_session_file)))


if __name__=='__main__':main()
