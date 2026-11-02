# SPDX-License-Identifier: GPL-2.0-only
# Private Full33 candidate feeder, derived from project replay script.
#!/usr/bin/env python3
"""Indexed timed MJPEG replay through the unchanged live queue/DPU/DMA/GLES.

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


def load_preview(args):
    # Same matched-public entry and spawn target as run_hybrid_replay.py.
    import importlib.util
    sys.path.insert(0,str(args.package/'src'))
    path=args.public_entry/'run_replay.py'
    spec=importlib.util.spec_from_file_location('indexed_matched_public_replay',path)
    replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)
    import posepriv_coach.field_preview_full33_split as entry
    if args.hybrid_profile_dir is not None:
        sys.path.insert(0,str(args.hybrid_profile_dir))
        from hybrid_entry import run_hybrid_preview
        entry._run=run_hybrid_preview
    else:
        from functools import partial
        from posepriv_coach.full33_preview_spawn import run_full33_preview
        entry._run=partial(run_full33_preview,entry_directory=str(args.public_entry))
    return entry.LivePreview


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-entry',type=Path,required=True)
    parser.add_argument('--hybrid-profile-dir',type=Path)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--client',choices=('poll','stream','status','none'),required=True)
    parser.add_argument('--front-rotation',type=int,default=90)
    parser.add_argument('--side-rotation',type=int,default=270)
    parser.add_argument('--queue-input',action='store_true',help='Use old JPEG pipe for transport comparison')
    parser.add_argument('--port',type=int,default=18765)
    parser.add_argument('--duration',type=float,help='Explicit cyclic replay duration seconds; default is exactly one original sequence')
    parser.add_argument('--schedule',type=Path,help='Independent original/cycle/target JSONL; default next to output')
    parser.add_argument('--presentation-capacity',type=int,help='Only when explicit, override benchmark_presentation_capacity, e.g.32768')
    args=parser.parse_args()
    from indexed_source import load_records,IndexedJpegs,Schedule
    LivePreview=load_preview(args)
    records=load_records(args.input);first=records[0].timestamp_ns
    plan=Schedule(records,duration_ns=None if args.duration is None else int(round(args.duration*1_000_000_000)))
    schedule_path=args.schedule or args.output.with_name(args.output.stem+'.schedule.private.jsonl')
    preview_config=args.config
    if args.presentation_capacity is not None:
        if args.presentation_capacity<1:raise ValueError('positive presentation capacity required')
        configured=json.loads(args.config.read_text());configured['benchmark_presentation_capacity']=args.presentation_capacity
        preview_config=args.output.with_name(args.output.stem+'.indexed-config.private.json')
        preview_config.write_text(json.dumps(configured,indent=2)+'\n')
    stop=threading.Event();received=[];client_errors=[];poll_seen=set()
    front_rotation,side_rotation=args.front_rotation,args.side_rotation
    preview=LivePreview(preview_config,args.port,front_rotation,side_rotation,shared_input=not args.queue_input)
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
    browser=None;run_error=None;submit_delays=[];submitted=0
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
        with IndexedJpegs(args.input)as images,schedule_path.open('w')as schedule:
            start=time.monotonic_ns()+100_000_000
            schedule.write(json.dumps(dict(kind='header',replay_start_ns=start,**plan.metadata()))+'\n')
            for event in plan.events():
                row=event.record;original,view,fid=row.timestamp_ns,row.view,row.frame_id
                jpeg=images.read(row)
                if fid%15==0 and not preview.process.is_alive():raise RuntimeError('Preview stopped before input ended')
                target=start+event.offset_ns
                delay=(target-time.monotonic_ns())/1e9
                if delay>0:time.sleep(delay)
                submitted_ns=time.monotonic_ns()
                submit_delays.append(submitted_ns-target)
                if args.duration is None:preview.submit(view,fid,target,jpeg)
                else:preview.submit(view,fid,target,jpeg,capture_epoch=event.cycle)
                submitted+=1
                schedule.write(json.dumps(dict(kind='source',cycle=event.cycle,source_index=event.source_index,
                    view=view,capture_epoch=None if args.duration is None else event.cycle,
                    original_frame_id=fid,original_timestamp_ns=original,offset=row.offset,length=row.length,
                    rebased_target_ns=target,submitted_ns=submitted_ns))+'\n')
                if args.client!='none'and fid%3==0:
                    state=json.load(urlopen(url+'/status.json',timeout=2))
                    n=state.get('frames',0)
                    if n and n not in seen:
                        seen.add(n);samples.append(dict(state,test_received_ns=time.monotonic_ns()))
                    if state.get('status')=='failed':raise RuntimeError(state.get('error'))
            schedule.write(json.dumps(dict(kind='footer',submitted_source_frames=submitted,planned_source_frames=plan.planned_frames,input_eof=True))+'\n')
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
        logical_side_file='side',client=args.client,input_frames=plan.planned_frames,duration_s=plan.last_offset_ns/1e9,
        original_input_frames=len(records),submitted_input_frames=submitted,source_schedule=str(schedule_path),
        replay_source_policy=plan.metadata(),preview_config=str(preview_config),presentation_capacity_override=args.presentation_capacity,
        replay_start_ns=locals().get('start'),run_error=run_error,final=final,samples=samples,client_received_ns=received,client_errors=client_errors,submit_delay_ns=submit_delays)
    args.output.write_text(json.dumps(result,indent=2))
    print(json.dumps(dict(frames=final['frames'],status=final['status'],error=final.get('error'),client_frames=len(received),client_errors=client_errors)),flush=True)
    if final['status']!='complete'or client_errors or run_error:raise SystemExit(1)


if __name__=='__main__':main()
