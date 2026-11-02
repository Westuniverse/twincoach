# SPDX-License-Identifier: GPL-2.0-only
"""Explicit child-only JPEG selection; original physical records stay attached."""
from time import monotonic_ns
import cv2
import numpy as np
from posepriv_coach.native_jpeg_decode import ThreadLocalJpegDecoder,UnsupportedJpeg


def run_stream_owner(cfg,*args):
    from stream_process import run_stream_owner as original_owner
    if not cfg.get('preview_jpeg_library'):return original_owner(cfg,*args)
    import posepriv_coach.field_preview_pipeline as pipeline
    from posepriv_coach.capture import CapturedFrame
    from posepriv_coach.contracts import CameraView
    decoder=ThreadLocalJpegDecoder(cfg['preview_jpeg_library'])
    original_decode=pipeline.decode_preview_frame
    scales={cv2.IMREAD_COLOR:1,cv2.IMREAD_REDUCED_COLOR_2:2,cv2.IMREAD_REDUCED_COLOR_4:4}
    def decode_frame(view,record,rotation,decode_flag,target=None):
        frame_id,timestamp,jpeg=record;started=monotonic_ns()
        try:image=decoder.decode(jpeg,scale=scales[decode_flag],rotation=rotation)
        except UnsupportedJpeg:return original_decode(view,record,rotation,decode_flag,target)
        if target is not None:
            destination=target(view,image.shape);np.copyto(destination,image);image=destination
        captured=CapturedFrame(view=CameraView(view),frame_id=frame_id,timestamp_ns=timestamp,
            sync_timestamp_ns=timestamp,width=image.shape[1],height=image.shape[0],pixel_format='bgr8',payload=image)
        finished=monotonic_ns()
        return captured,finished-started,finished
    import stream_runtime
    original_infer=stream_runtime.infer_latest_pair
    if cfg.get('preview_completion_first_decode',False):
        from completion_first import infer_latest_pair
        stream_runtime.infer_latest_pair=infer_latest_pair
    pipeline.decode_preview_frame=decode_frame
    try:return original_owner(cfg,*args)
    finally:
        # Executor threads have joined inside original_owner; their private
        # handles have gone away before restoring this process-local selector.
        pipeline.decode_preview_frame=original_decode;decoder.close()
        stream_runtime.infer_latest_pair=original_infer
