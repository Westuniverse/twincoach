"""Temporarily pause only the configured recorded demo; preserve its exact argv."""
from pathlib import Path
import os
import signal
import subprocess
import time


def pause_recorded_demo(config):
    if not config:return lambda:None
    control=Path(config['control']);pidfile=control/'primary.pid'
    if not pidfile.exists():return lambda:None
    original=pidfile.read_text();pid=int(original)
    proc=Path('/proc')/str(pid)
    try:args=[v.decode()for v in(proc/'cmdline').read_bytes().split(b'\0')if v]
    except FileNotFoundError:return lambda:None
    if config['script']not in args or config['video']not in args:
        raise RuntimeError('Current recorded-demo PID is another program; leave it untouched')
    if proc.stat().st_uid!=os.getuid():raise RuntimeError('Recorded demo belongs to another user')
    os.kill(pid,signal.SIGTERM)
    for _ in range(100):
        if not proc.exists()or(proc/'stat').read_text().split()[2]=='Z':break
        time.sleep(.02)
    else:raise RuntimeError('Recorded demo did not stop')
    def restore():
        if pidfile.read_text()!=original:
            raise RuntimeError('Recorded-demo owner changed during capture; do not start a duplicate')
        with(control/'primary.log').open('a')as log:
            child=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                env=dict(os.environ,DISPLAY=':0',XAUTHORITY='/run/user/1000/gdm/Xauthority'),start_new_session=True)
        pidfile.write_text(str(child.pid)+'\n')
    return restore
