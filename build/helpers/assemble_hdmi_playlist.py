#!/usr/bin/env python3
"""Assemble private comparison exports/raw captures at their recorded speed."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from contextlib import ExitStack
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def causal_frame_indices(timestamps,fps):
    """CFR samples use only a past frame; include the final source frame."""
    stamps=tuple(timestamps)
    if (not math.isfinite(fps) or fps<=0 or not stamps or
            any(type(t) is not int or t<0 for t in stamps) or
            any(b<a for a,b in zip(stamps,stamps[1:]))):
        raise ValueError('finite positive fps and ordered nonnegative integer timestamps required')
    first=stamps[0]
    count=math.ceil((stamps[-1]-first)*fps/1e9)+1
    return tuple(max(0,bisect_right(stamps,first+round(i*1e9/fps))-1) for i in range(count))


def append_comparison(writer,video,report,fps):
    import cv2
    records=report['source_frames']
    if report['review_frames']<0 or report['video_frames']!=len(records)+report['review_frames']:
        raise ValueError('comparison report source/review frame counts differ')
    schedule=causal_frame_indices([r['front_timestamp_ns'] for r in records],fps)
    capture=cv2.VideoCapture(str(video))
    if not capture.isOpened():raise ValueError(f'unreadable comparison: {video}')
    current=-1;canvas=None;written=0
    try:
        for index in schedule:
            while current<index:
                ok,canvas=capture.read();current+=1
                if not ok or canvas.shape[:2]!=(720,1280):
                    raise ValueError('comparison is truncated or has a different canvas size')
            writer.write(canvas);written+=1
        # Every exported source and review frame must actually decode.
        while current+1<report['video_frames']:
            ok,last=capture.read();current+=1
            if not ok or last.shape[:2]!=(720,1280):raise ValueError('comparison review is truncated')
        if capture.read()[0]:raise ValueError('comparison has unreported frames')
        for _ in range(round(report['review_frames']/report['export_fps']*fps)):
            writer.write(canvas);written+=1
        return written
    finally:capture.release()


def append_raw(writer,directory,views,fps,title):
    import cv2
    import numpy as np
    from posepriv_coach.indexed_mjpeg import load_mjpeg_index
    from posepriv_coach.text_overlay import OptionalCjkTextOverlay,OverlayText,default_cjk_font
    overlay=OptionalCjkTextOverlay(default_cjk_font(),numpy_module=np)
    with ExitStack() as stack:
        indexes={};streams={}
        for view in views:
            path=directory/f'{view}.mjpeg'
            indexes[view]=load_mjpeg_index(directory/f'{view}.timestamps.jsonl',video_size=path.stat().st_size)
            streams[view]=stack.enter_context(path.open('rb'))
        first=min(rows[0].timestamp_ns for rows in indexes.values())
        last=max(rows[-1].timestamp_ns for rows in indexes.values())
        frames=math.ceil((last-first)*fps/1e9)+1
        times={view:[r.timestamp_ns for r in rows] for view,rows in indexes.items()}
        cache={};decoded={view:0 for view in views}
        def decode(view,index):
            row=indexes[view][index];stream=streams[view];stream.seek(row.offset)
            data=stream.read(row.length)
            if len(data)!=row.length or data[:2]!=b'\xff\xd8' or data[-2:]!=b'\xff\xd9':
                raise ValueError(f'broken indexed JPEG {view}/{row.frame_id}')
            frame=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
            if frame is None:raise ValueError(f'undecodable indexed frame {view}/{row.frame_id}')
            return cv2.rotate(frame,cv2.ROTATE_90_COUNTERCLOCKWISE if view=='front' else cv2.ROTATE_90_CLOCKWISE)
        for sample in range(frames):
            target=first+round(sample*1e9/fps)
            canvas=np.full((720,1280,3),(25,25,32),np.uint8)
            for column,view in enumerate(('front','side')):
                if view not in indexes:continue
                index=bisect_right(times[view],target)-1
                if index<0:continue
                if cache.get(view,(-1,None))[0]!=index:
                    for item in range(decoded[view],index+1):frame=decode(view,item)
                    decoded[view]=index+1;cache[view]=(index,frame)
                frame=cache[view][1]
                scale=min(600/frame.shape[1],580/frame.shape[0])
                w,h=round(frame.shape[1]*scale),round(frame.shape[0]*scale)
                x=column*640+(640-w)//2;y=100+(580-h)//2
                canvas[y:y+h,x:x+w]=cv2.resize(frame,(w,h))
            overlay.draw_many(canvas,[OverlayText(title,28,12,26,(240,240,240)),
                OverlayText('采集原片 · 尚无姿态分析与动作评分',28,52,22,(80,200,255)),
                OverlayText('正面' if 'front' in views else '正面文件缺失',28,105,22,(220,220,220)),
                OverlayText('侧面',668,105,22,(220,220,220))])
            writer.write(canvas)
        return frames


def chapter_card(writer,title,kind,fps):
    import numpy as np
    from posepriv_coach.text_overlay import OptionalCjkTextOverlay,OverlayText,default_cjk_font
    canvas=np.full((720,1280,3),(25,25,32),np.uint8)
    overlay=OptionalCjkTextOverlay(default_cjk_font(),numpy_module=np)
    overlay.draw_many(canvas,[OverlayText('PosePriv 运动教练',70,170,42,(240,240,240)),
        OverlayText(title,70,290,32,(180,225,230)),
        OverlayText('完整录像 · 数字人转换与深度—角度评分' if kind=='comparison'
                    else '采集测试原片 · 未进行姿态分析和评分',70,365,25,(140,175,195)),
        OverlayText('按源时间回放',70,440,22,(140,175,195))])
    count=round(2*fps)
    for _ in range(count):writer.write(canvas)
    return count


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--playlist',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--fps',type=float,default=30)
    args=parser.parse_args(argv)
    if not math.isfinite(args.fps) or args.fps<=0:parser.error('fps must be positive')
    if args.output_dir.exists():parser.error('keep old videos: output directory exists')
    entries=json.loads(args.playlist.read_text())
    if not isinstance(entries,list) or not entries:parser.error('playlist must be a nonempty list')
    import cv2
    args.output_dir.mkdir(parents=True)
    writer=cv2.VideoWriter(str(args.output_dir/'complete-playlist.avi'),
        cv2.VideoWriter_fourcc(*'MJPG'),args.fps,(1280,720))
    if not writer.isOpened():raise RuntimeError('could not open AVI writer')
    chapters=[];total=0
    try:
        for entry in entries:
            card_frames=chapter_card(writer,entry['title'],entry['kind'],args.fps)
            start=total;total+=card_frames
            if entry['kind']=='comparison':
                directory=Path(entry['directory'])
                report=json.loads((directory/'render.json').read_text())
                count=append_comparison(writer,directory/report.get('video_file','comparison.mp4'),report,args.fps)
            elif entry['kind']=='raw_indexed':
                count=append_raw(writer,Path(entry['directory']),entry['views'],args.fps,entry['title'])
            else:raise ValueError('unknown playlist entry kind')
            chapters.append(dict(title=entry['title'],kind=entry['kind'],start_frame=start,
                content_start_frame=total,title_frames=card_frames,frames=count+card_frames,
                start_seconds=start/args.fps,duration_seconds=(count+card_frames)/args.fps))
            total+=count
            print(json.dumps(chapters[-1],ensure_ascii=False),flush=True)
    finally:writer.release()
    (args.output_dir/'playlist-report.json').write_text(json.dumps(dict(
        scope='offline timestamp-preserving replay, not interpolation or live FPGA FPS',
        fps=args.fps,width=1280,height=720,frames=total,duration_seconds=total/args.fps,
        chapters=chapters),ensure_ascii=False,indent=2)+'\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
