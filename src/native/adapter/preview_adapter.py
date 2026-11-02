# SPDX-License-Identifier: GPL-2.0-only
"""Private stateless Full33 single-pose decoder; explicit per-image official ROI.

Semantics transcribed from pinned Apache-2.0 v0.10.18 calculators in upstream/.
This is before VIDEO temporal smoothing/association; no detector/default ROI.
"""
from dataclasses import dataclass
import math
import numpy as np

from posepriv_coach.avatar_image_source import AvatarImageSource
from posepriv_coach.contracts import CameraView, Keypoint, PoseFrame
from posepriv_coach.world_avatar_preview import WORLD_JOINTS, WorldDisplayObservation
from posepriv_coach.preview_head_orientation import (
    CurrentHeadOrientation, world_face_plane_rotation, world_body_rotation)


@dataclass(frozen=True)
class FrameRoi:
    """Official NORM_RECT and LETTERBOX_PADDING for the *same* prepared input.

    Source dimensions refer to the oriented source image used by preprocessing.
    Rotation is ROI radians in that image, not source.rotation_degrees.
    """
    rect: tuple  # center_x,center_y,width,height,rotation; normalized image coords
    padding: tuple  # left,top,right,bottom; normalized tensor padding

    def __post_init__(self):
        if (len(self.rect) != 5 or len(self.padding) != 4 or
                not all(math.isfinite(x) for x in self.rect+self.padding) or
                min(self.rect[2:4]) <= 0 or
                self.padding[0]+self.padding[2] >= 1 or
                self.padding[1]+self.padding[3] >= 1):
            raise ValueError('one finite positive explicit ROI and letterbox required')


@dataclass(frozen=True)
class TailHeads:
    """Driver outputs, useful bytes only, with original source wire identity."""
    identity: tuple
    landmarks195_q5: bytes
    world117_q14: bytes
    presence1_q8: bytes


@dataclass(frozen=True)
class DecodedFull33:
    source: AvatarImageSource
    pose: PoseFrame
    world: WorldDisplayObservation
    auxiliary2: tuple
    presence_score: float

    def head_orientation(self):
        """Existing world-face/body inference; no guessed head or confidence fill."""
        points = self.world.world_landmarks
        return CurrentHeadOrientation(self.source.frame_id, self.source.timestamp_ns,
            'world_face_plane', world_face_plane_rotation(points), world_body_rotation(points))

    def next_roi(self):
        """AlignmentPointsRects then square_long/scale1.25, official aux33:35."""
        if not self.auxiliary2:
            return None
        center, scale = self.auxiliary2
        dx = (scale['x']-center['x'])*self.source.width
        dy = (scale['y']-center['y'])*self.source.height
        side = 2*math.hypot(dx, dy)*1.25
        angle = math.pi/2-math.atan2(-dy, dx)
        angle -= 2*math.pi*math.floor((angle+math.pi)/(2*math.pi))
        # Return only NORM_RECT: next preprocessing must supply actual padding.
        return (center['x'], center['y'], side/self.source.width,
                side/self.source.height, angle)


def identity_for(source):
    return (source.frame_id, source.timestamp_ns, {'front':1,'side':2}[source.view],
            source.width, source.height)


def _sigmoid(value):
    with np.errstate(over='ignore'):
        return np.float32(1)/(np.float32(1)+np.exp(-np.asarray(value, np.float32)))


_HEAT_SIGMOID_Q2 = _sigmoid(np.arange(-128,128,dtype=np.float32)/np.float32(4))


def refine39(raw195, heat_q2):
    """Official kernel7, default min.5, no visibility/presence refinement.

    Heatmap lookup applies sigmoid only to the sampled7x7 regions. Full159744
    heatmap float dequantization/activation is avoided; source heat bytes stay raw.
    """
    points = np.asarray(raw195, np.float32).reshape(39,5).copy()
    points[:,:3] /= np.float32(256)
    points[:,3:] = _sigmoid(points[:,3:])
    heat = np.frombuffer(heat_q2,dtype=np.int8).reshape(64,64,39)
    for i in range(39):
        col = int(points[i,0]*np.float32(64))  # C++ int truncates toward zero
        row = int(points[i,1]*np.float32(64))
        if not (0 <= col < 64 and 0 <= row < 64):
            continue
        l,r,t,b = max(0,col-3),min(64,col+4),max(0,row-3),min(64,row+4)
        weights = _HEAT_SIGMOID_Q2[heat[t:b,l:r,i].astype(np.int16)+128]
        total = weights.sum(dtype=np.float32)
        if weights.max() >= np.float32(.5) and total > 0:
            points[i,0] = ((weights*np.arange(l,r,dtype=np.float32)).sum(dtype=np.float32)/np.float32(64))/total
            points[i,1] = ((weights*np.arange(t,b,dtype=np.float32)[:,None]).sum(dtype=np.float32)/np.float32(64))/total
    return points


def project(points, roi):
    """Official letterbox removal + square-ROI no-IMAGE_DIMENSIONS projection."""
    out = points.copy()
    left,top,right,bottom = map(np.float32,roi.padding)
    out[:,0] = (out[:,0]-left)/(np.float32(1)-(left+right))
    out[:,1] = (out[:,1]-top)/(np.float32(1)-(top+bottom))
    out[:,2] /= np.float32(1)-(left+right)
    cx,cy,width,height,angle = map(np.float32,roi.rect)
    x,y = out[:,0]-np.float32(.5), out[:,1]-np.float32(.5)
    cosine,sine = np.cos(angle),np.sin(angle)
    out[:,0] = (cosine*x-sine*y)*width+cx
    out[:,1] = (sine*x+cosine*y)*height+cy
    out[:,2] *= width
    return out


def _records(points):
    return tuple(dict(zip(('x','y','z','visibility','presence'),map(float,p))) for p in points)


def decode(source, roi, heads, heat_q2):
    """Raw S16 head outputs + exact native heatmap -> common17 + extra world33."""
    if heads.identity != identity_for(source):
        raise ValueError('tail source/frame/ns/geometry mismatch')
    if (len(heads.landmarks195_q5),len(heads.world117_q14),len(heads.presence1_q8),len(heat_q2)) != (390,234,2,159744):
        raise ValueError('tail/head useful byte contract changed')
    raw_presence = np.frombuffer(heads.presence1_q8,dtype='<i2')[0]
    score = float(_sigmoid(np.float32(raw_presence)/np.float32(256)))
    raw = np.frombuffer(heads.landmarks195_q5,dtype='<i2').astype(np.float32)/np.float32(32)
    world = np.frombuffer(heads.world117_q14,dtype='<i2').astype(np.float32)/np.float32(16384)
    return decode_values(source,roi,raw,world,score,heat_q2)


def decode_values(source, roi, landmarks195, world117, presence_probability, heat_q2):
    """Same decoder for already-dequantized cached heads; no logit reconstruction."""
    score = float(presence_probability)
    # Official ThresholdingCalculator is strict > min_detection_confidence=.5.
    if score <= .5:
        pose = PoseFrame(CameraView(source.view),source.frame_id,source.timestamp_ns,{},source.width,source.height)
        return DecodedFull33(source,pose,WorldDisplayObservation(source,(),()),(),score)
    refined = refine39(landmarks195,heat_q2)
    image = project(refined[:35],roi)
    world = np.empty((33,5),np.float32)
    world[:,:3] = np.asarray(world117,np.float32).reshape(39,3)[:33]
    # World projection is rotation only, never crop scale/translation/z division.
    angle = np.float32(roi.rect[4]);c,s = np.cos(angle),np.sin(angle)
    x,y = world[:,0].copy(),world[:,1].copy()
    world[:,0],world[:,1] = c*x-s*y,s*x+c*y
    world[:,3:] = refined[:33,3:]
    normalized, world_points = _records(image[:33]), _records(world)
    pose = PoseFrame(CameraView(source.view),source.frame_id,source.timestamp_ns,
        {name:Keypoint(normalized[i]['x'],normalized[i]['y'],
                       min(normalized[i]['visibility'],normalized[i]['presence']))
         for i,name in WORLD_JOINTS.items()},source.width,source.height)
    return DecodedFull33(source,pose,WorldDisplayObservation(source,world_points,normalized),
                         _records(image[33:35]),score)


@dataclass(frozen=True)
class PreparedImage:
    source: AvatarImageSource
    roi: FrameRoi
    input_q7: bytes


class PairedPreviewAdapter:
    """Single prefix owner; decoder/tail on reader owner, next prefix may overlap.

    tail_execute(cuts,source) must finish its real35op/lastB and return TailHeads.
    PreparedImage carries caller's exact ROI/padding, never an inferred fallback.
    """
    def __init__(self, prefix_worker, tail_execute):
        self.prefix_worker,self.tail_execute = prefix_worker,tail_execute

    def infer_pair(self, prepared, executor):
        prepared = tuple(prepared)
        if len(prepared) != 2:
            raise ValueError('exactly two prepared physical views required')
        result = []
        with self.prefix_worker.pipelined([identity_for(p.source) for p in prepared],
                (p.input_q7 for p in prepared),executor) as responses:
            for request,cuts in zip(prepared,responses,strict=True):
                heads = self.tail_execute(cuts,request.source)
                result.append(decode(request.source,request.roi,heads,cuts['heat233']))
        return tuple(result)
