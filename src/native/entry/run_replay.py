# SPDX-License-Identifier: GPL-2.0-only
# Private Full33 candidate feeder, derived from project replay script.
#!/usr/bin/env python3
"""Timed MJPEG replay through the live queue/DPU/DMA/GLES, without a camera.

This rebases recorded times onto a test monotonic clock; it is not UVC capture
or raw recording throughput. Source IDs and original timestamps remain in input.
Run under the existing shared-DMA supervisor; model/assets remain in private config.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import urlopen


HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent.parent/'gl'),str(HERE.parent.parent/'world')]
sys.path[:0]=[str(HERE),str(HERE.parent.parent/'actor'),str(HERE.parent.parent/'process'),str(HERE.parent.parent/'geometry')]
import posepriv_coach
posepriv_coach.__path__.insert(0,str(HERE))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--client',choices=('poll','stream','status','none'),required=True)
    parser.add_argument('--front-rotation',type=int,default=90)
    parser.add_argument('--side-rotation',type=int,default=270)
    parser.add_argument('--queue-input',action='store_true',help='Use old JPEG pipe for transport comparison')
    parser.add_argument('--port',type=int,default=18765)
    args=parser.parse_args()
    sys.path.insert(0,str(args.package/'src'))
    import posepriv_coach
    posepriv_coach.__path__.append(str(Path(__file__).resolve().parent))
    from posepriv_coach.field_preview_full33_split import LivePreview
    records=[]
    for view in ('front','side'):
        with(args.input/(view+'.mjpeg')).open('rb')as images:
            for line in(args.input/(view+'.timestamps.jsonl')).read_text().splitlines():
                row=json.loads(line);images.seek(row['offset']);jpeg=images.read(row['length'])
                if len(jpeg)!=row['length']:raise ValueError('Truncated replay image')
                logical=view
                records.append((row['timestamp_ns'],logical,row['frame_id'],jpeg))
    records.sort(key=lambda r:(r[0],r[1]));first=records[0][0]
    stop=threading.Event();received=[];client_errors=[];poll_seen=set()
    front_rotation,side_rotation=args.front_rotation,args.side_rotation
    preview=LivePreview(args.config,args.port,front_rotation,side_rotation,shared_input=not args.queue_input)
    url=f'http://127.0.0.1:{args.port}';samples=[];seen=set()
    def client():
        try:
            if args.client=='stream':
                with urlopen(url+'/stream.mjpg',timeout=15)as response:
                    while not stop.is_set():
                        boundary=response.readline()
                        if not boundary:break
                        if boundary!=b'--posepriv\r\n':raise ValueError('Invalid MJPEG boundary')
                        length=None
                        while True:
                            line=response.readline()
                            if line==b'\r\n':break
                            if not line:raise ValueError('Truncated MJPEG headers')
                            if line.lower().startswith(b'content-length:'):length=int(line.split(b':',1)[1])
                        if length is None:raise ValueError('Missing MJPEG size')
                        image=response.read(length);ending=response.read(2)
                        if len(image)!=length or ending!=b'\r\n':raise ValueError('Truncated MJPEG frame')
                        received.append(time.monotonic_ns())
            else:
                while not stop.is_set():
                    t=time.monotonic()
                    state=json.load(urlopen(url+'/status.json',timeout=2))
                    if args.client=='poll'and state.get('status')=='running'and state['source_age_ms']<=1000:
                        with urlopen(url+'/frame.jpg',timeout=2)as response:response.read()
                        if state["frames"]not in poll_seen:
                            poll_seen.add(state["frames"]);received.append(time.monotonic_ns())
                    stop.wait(max(0,.2-(time.monotonic()-t)))
        except Exception as exc:
            if not stop.is_set():client_errors.append(f'{type(exc).__name__}: {exc}')
    browser=None;run_error=None;submit_delays=[]
    try:
        deadline=time.monotonic()+20
        while True:
            try:
                with urlopen(url+'/status.json',timeout=1)as response:json.load(response)
                break
            except OSError:
                if time.monotonic()>=deadline:raise TimeoutError('Preview HTTP did not start')
                time.sleep(.1)
        if args.client!='none':
            browser=threading.Thread(target=client,daemon=True);browser.start()
        start=time.monotonic_ns()+100_000_000
        for original,view,fid,jpeg in records:
            if fid%15==0 and not preview.process.is_alive():raise RuntimeError('Preview stopped before input ended')
            target=start+original-first
            delay=(target-time.monotonic_ns())/1e9
            if delay>0:time.sleep(delay)
            submit_delays.append(time.monotonic_ns()-target);preview.submit(view,fid,target,jpeg)
            if args.client!='none'and fid%3==0:
                state=json.load(urlopen(url+'/status.json',timeout=2))
                n=state.get('frames',0)
                if n and n not in seen:
                    seen.add(n);samples.append(dict(state,test_received_ns=time.monotonic_ns()))
                if state.get('status')=='failed':raise RuntimeError(state.get('error'))
        time.sleep(.6)
    except BaseException as exc:
        run_error=f'{type(exc).__name__}: {exc}'
    finally:
        stop.set();final=preview.close()
        if browser is not None:browser.join(timeout=5)
    backend=json.loads(args.config.read_text()).get('preview_pose_backend','movenet')
    result=dict(scope='recorded real-time replay, test clocks rebased; explicit pose backend plus actual PPS4/Mali and on-board loopback HTTP client; no cameras, UVC, raw SSH recording, Windows browser or physical scanout measured',
        pose_backend=backend,
        logical_front_file='front',
        logical_side_file='side',client=args.client,input_frames=len(records),duration_s=(records[-1][0]-first)/1e9,
        replay_start_ns=locals().get('start'),run_error=run_error,final=final,samples=samples,client_received_ns=received,client_errors=client_errors,submit_delay_ns=submit_delays)
    args.output.write_text(json.dumps(result,indent=2))
    print(json.dumps(dict(frames=final['frames'],status=final['status'],error=final.get('error'),client_frames=len(received),client_errors=client_errors)),flush=True)
    if final['status']!='complete'or client_errors or run_error:raise SystemExit(1)


if __name__=='__main__':main()
