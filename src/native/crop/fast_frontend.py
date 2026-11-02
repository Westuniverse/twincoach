# SPDX-License-Identifier: GPL-2.0-only
"""BGR-direct current-frame frontend; only cold asynchronous detector uses full RGB."""
from pathlib import Path
import sys
import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tail4_hdmi_candidate_20261030'))
from preview_runtime import AsyncViewFrontend
from live_frontend import alignment_roi
from preview_adapter import PreparedImage


class FastBgrViewFrontend(AsyncViewFrontend):
    def __init__(self,broker,cropper):
        super().__init__(broker);self.cropper=cropper

    def prepare(self,source,bgr,epoch):
        f=self.frontend;old=f.previous
        if f.pending is not None:raise ValueError('finish/cancel pending source before prepare')
        if bgr.dtype!=np.uint8 or bgr.shape!=(source.height,source.width,3):
            raise ValueError('oriented BGR image must match original source')
        if old is not None:
            if epoch==f.epoch and source.timestamp_ns<old.timestamp_ns:
                raise ValueError('source moved backwards within capture epoch')
            if (self.broker.geometry(old)!=self.broker.geometry(source) or epoch!=f.epoch
                    or source.timestamp_ns-old.timestamp_ns>250_000_000):f.rect=None
        if f.rect is None:
            detections=self.broker.take(source,epoch)
            if detections:
                rect=alignment_roi(detections[0].keypoints4,source.width,source.height)
                if min(rect[2:4])>0:f.rect=rect
            if f.rect is None:
                if source.view not in self.broker.jobs:
                    self.broker.submit(source,cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB),epoch)
                f.previous,f.epoch=source,epoch
                return None
        q7,roi=self.cropper.crop(bgr,f.rect)
        f.previous,f.epoch=source,epoch
        f.pending=PreparedImage(source,roi,q7)
        return f.pending
