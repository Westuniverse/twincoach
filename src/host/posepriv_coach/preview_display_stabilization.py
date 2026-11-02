# SPDX-License-Identifier: GPL-2.0-only
"""Display-only adaptive vector One Euro filter; original implementation.

Algorithm: Casiez, Roussel, Vogel, CHI 2012, https://gery.casiez.net/1euro/ .
No external implementation copied. No prediction, holds, synthetic valid points,
confidence edits, source edits, or feedback to raw scorer / optical flow.
"""
from dataclasses import replace
from math import isfinite,pi,sqrt
from posepriv_coach.contracts import Keypoint
from posepriv_coach.pose_adapter import MOVENET_COCO_KEYPOINTS as NAMES


class AdaptiveDisplayPoseStabilizer:
    def __init__(self,min_cutoff=5.,beta=30.,derivative_cutoff=1.,max_gap_ns=200_000_000):
        self.min_cutoff=float(min_cutoff);self.beta=float(beta)
        self.derivative_cutoff=float(derivative_cutoff);self.max_gap_ns=max_gap_ns
        self._views={}

    def reset(self):self._views.clear()

    def apply(self,pose,*,epoch=''):
        """Return display points with independent per-view vector history.

        Missing/invalid points pass through and reacquire at current data.
        Each point shares its x/y cutoff; raw pose/source/confidence are retained.
        """
        names=tuple(name for name in NAMES if name in pose.keypoints)
        if not names:
            self._views.pop(pose.view,None)
            return pose
        identity=(epoch,pose.source_width,pose.source_height,names)
        state=self._views.get(pose.view)
        continuing=state is not None and state[0]==identity and 0<pose.timestamp_ns-state[1]<=self.max_gap_ns
        if not continuing:
            # Each point stores current raw, filtered, derivative and validity.
            state=[identity,pose.timestamp_ns,[[0.,0.,0.,0.,0.,0.,False]for _ in names]]
            self._views[pose.view]=state
        else:
            dt=(pose.timestamp_ns-state[1])*1e-9
            ad=dt/(dt+1./(2.*pi*self.derivative_cutoff))
            state[1]=pose.timestamp_ns
        output=dict(pose.keypoints)
        for name,saved in zip(names,state[2]):
            point=pose.keypoints[name];x,y=float(point.x),float(point.y)
            valid=float(point.confidence)>=.5 and isfinite(x)and isfinite(y)and 0<=x<=1 and 0<=y<=1
            if continuing and valid and saved[6]:
                dx=saved[4]+ad*((x-saved[0])/dt-saved[4])
                dy=saved[5]+ad*((y-saved[1])/dt-saved[5])
                cutoff=self.min_cutoff+self.beta*sqrt(dx*dx+dy*dy)
                alpha=dt/(dt+1./(2.*pi*cutoff))
                fx=saved[2]+alpha*(x-saved[2]);fy=saved[3]+alpha*(y-saved[3])
            else:fx,fy,dx,dy=x,y,0.,0.
            saved[0]=x;saved[1]=y;saved[2]=fx;saved[3]=fy;saved[4]=dx;saved[5]=dy;saved[6]=valid
            if valid:output[name]=Keypoint(fx,fy,point.confidence)
        return replace(pose,keypoints=output)
