"""The existing JPEG/point-overlay algorithm, with explicit bounded RGB reuse.

The caller owns streams and serializes each view. Front and side may run in
parallel, but all current futures must finish before the next display frame.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from posepriv_coach.skeleton_display import SKELETON_EDGES
from read_ahead import VideoReadAhead


class IndexedPreviewImages:
    """Prepare pixels from ``{id(chapter): {view: open_stream}}`` ownership maps."""

    def __init__(self, chapter_streams, *, reduced_jpeg=1, reuse=False, read_ahead=False, rgba=False, native_resize_library=None):
        if reduced_jpeg not in (1,2,4):
            raise ValueError('reduced_jpeg must be 1, 2 or 4')
        self._closed=False
        self._resizers={}
        if native_resize_library is not None:
            from posepriv_coach.native_preview_resize import SerializedRotatedPreviewResize
            try:
                for view in ('front','side'):
                    self._resizers[view]=SerializedRotatedPreviewResize(native_resize_library)
            except Exception:
                self.close()
                raise
        self.chapter_streams=chapter_streams
        self.decode_mode={1:cv2.IMREAD_COLOR,2:cv2.IMREAD_REDUCED_COLOR_2,
                          4:cv2.IMREAD_REDUCED_COLOR_4}[reduced_jpeg]
        self.reuse=reuse
        self.rgba=rgba
        self.read_ahead=VideoReadAhead() if read_ahead else None
        self._last={}
        blank=bytes((0,0,0,255))*(450*253) if rgba else bytes(450*253*3)
        self._blank=np.frombuffer(blank,np.uint8).reshape(450,253,4 if rgba else 3)

    def close(self):
        # Registered before pool.shutdown, hence called after workers join.
        for resizer in self._resizers.values():resizer.close()
        self._closed=True

    @staticmethod
    def _empty_timings(*, cache_hit=False, missing_view=False):
        return dict(decode_ns=0,read_advice_ns=0,read_ns=0,jpeg_decode_ns=0,rotate_resize_ns=0,points_rgb_ns=0,
                    cache_hit=cache_hit,missing_view=missing_view)

    def get(self, view, chapter, sample):
        if self._closed:raise RuntimeError('preview image reader is closed')
        if view not in ('front','side'):
            raise ValueError('front or side view required')
        if view not in sample.rows:
            self._last.pop(view,None)
            if self.read_ahead is not None:self.read_ahead.clear(view)
            return self._blank,self._empty_timings(missing_view=True)
        pair=getattr(sample,'pair',None)
        pose=getattr(pair,view) if pair is not None else None
        row=sample.rows[view]
        stream=self.chapter_streams[id(chapter)][view]
        rotation=chapter.rotation_degrees[view]
        markers=getattr(sample,'display_keypoints',{}).get(view)
        if markers is not None:
            source=markers.source
            if (source.view,source.frame_id,source.timestamp_ns,source.rotation_degrees)!=(
                    view,row.frame_id,row.timestamp_ns,rotation):
                raise ValueError('display markers and original image differ')
        points=markers.points if markers is not None else pose.keypoints if pose is not None else None
        marker_key=(tuple((n,p.x,p.y,p.confidence)for n,p in sorted(points.items()))
                    if markers is not None else None)
        key=(id(chapter),chapter.capture_dir,id(stream),row.frame_id,rotation,
             id(pose) if pose is not None else None,marker_key)
        previous=self._last.get(view)
        if self.reuse and previous is not None and previous[0]==key:
            return previous[2],self._empty_timings(cache_hit=True)
        stream.seek(row.offset)
        decode_start=time.perf_counter_ns()
        if self.read_ahead is not None:self.read_ahead.request(view,chapter,stream,row)
        advised=time.perf_counter_ns()
        data=stream.read(row.length)
        loaded=time.perf_counter_ns()
        image=cv2.imdecode(np.frombuffer(data,np.uint8),self.decode_mode)
        decode_end=time.perf_counter_ns()
        if image is None:
            raise ValueError(f'undecodable indexed JPEG {view}/{row.frame_id}')
        if rotation and not self._resizers:
            image=cv2.rotate(image,{90:cv2.ROTATE_90_CLOCKWISE,180:cv2.ROTATE_180,
                                    270:cv2.ROTATE_90_COUNTERCLOCKWISE}[rotation])
        height,width=image.shape[:2]
        if self._resizers and rotation in (90,270):height,width=width,height
        scale=min(253/width,450/height)
        w,h=max(1,round(width*scale)),max(1,round(height*scale))
        x0,y0=(253-w)//2,(450-h)//2
        if self._resizers:
            resized=np.empty((h,w,3),np.uint8)
            self._resizers[view](image,resized,rotation=rotation)
        else:
            resized=cv2.resize(image,(w,h))
        image=np.zeros((450,253,3),np.uint8)
        image[y0:y0+h,x0:x0+w]=resized
        resize_end=time.perf_counter_ns()
        if points is not None:
            for a,b in SKELETON_EDGES:
                pa,pb=points.get(a),points.get(b)
                if pa is not None and pb is not None and min(pa.confidence,pb.confidence)>=.5:
                    cv2.line(image,(x0+round(pa.x*w),y0+round(pa.y*h)),(x0+round(pb.x*w),y0+round(pb.y*h)),(80,230,130),2)
            for point in points.values():
                if point.confidence>=.5:
                    cv2.circle(image,(x0+round(point.x*w),y0+round(point.y*h)),3,(50,190,255),-1)
        rgb_image=cv2.cvtColor(image,cv2.COLOR_BGR2RGBA if self.rgba else cv2.COLOR_BGR2RGB)
        rgb_image=np.frombuffer(rgb_image.tobytes(),np.uint8).reshape(rgb_image.shape)
        end=time.perf_counter_ns()
        if self.reuse:
            # Retain chapter and pose so neither id can be recycled while this
            # last-result entry exists. There is at most one entry per view.
            self._last[view]=(key,chapter,rgb_image,pose)
        return rgb_image,dict(decode_ns=decode_end-decode_start,
                              read_advice_ns=advised-decode_start,read_ns=loaded-advised,
                              jpeg_decode_ns=decode_end-loaded,
                              rotate_resize_ns=resize_end-decode_end,
                              points_rgb_ns=end-resize_end,cache_hit=False,missing_view=False)
