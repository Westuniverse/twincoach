# SPDX-License-Identifier: GPL-2.0-only
"""Board-local detector/ROI/crop frontend for the fixed Full33 DPU+Tail4 path.

Official v0.10.18 algorithm/options are cached in upstream/. No Task VIDEO
association/smoothing claim. Each frontend owns one physical view's ROI state.
"""
from dataclasses import dataclass
import math
import numpy as np
import cv2

from preview_adapter import FrameRoi, PreparedImage, decode


def anchors2254():
    # fixed_anchor_size=true: all w/h=1; merged equal-stride layers contribute
    # 2,2,6 anchors per cell. Scale magnitudes disappear for fixed-size anchors.
    centers = []
    for grid, repeats in ((28,2),(14,2),(7,6)):
        for y in range(grid):
            for x in range(grid):
                centers.extend([((x+.5)/grid,(y+.5)/grid)]*repeats)
    return np.asarray(centers,np.float32)


ANCHORS = anchors2254()
# Existing native Fix input mode1: RGB0..1, Q7, floor(v*128+.5), S8 clamp.
Q7_RGB_LUT = np.clip(np.floor(np.arange(256,dtype=np.float32)/np.float32(255)*
                             np.float32(128)+np.float32(.5)), -128,127).astype(np.int8)


@dataclass(frozen=True)
class Detection:
    score: float
    box_xyxy: tuple
    keypoints4: tuple


def decode_detections(raw_boxes, raw_scores):
    """Official XYWH/2254/12 decode, score sigmoidclip100 and threshold .5."""
    raw = np.asarray(raw_boxes,np.float32).reshape(2254,12)
    logits = np.clip(np.asarray(raw_scores,np.float32).reshape(2254),-100,100)
    with np.errstate(over='ignore'):
        scores = np.float32(1)/(np.float32(1)+np.exp(-logits))
    # TensorsToDetections includes exact .5; final pose gate is separately >.5.
    selected = np.flatnonzero((scores >= np.float32(.5)) &
        (raw[:,2] >= 0) & (raw[:,3] >= 0))
    if not len(selected):
        return ()
    raw = raw[selected]
    centers = raw[:,:2]/np.float32(224)+ANCHORS[selected]
    size = raw[:,2:4]/np.float32(224)
    boxes = np.concatenate((centers-size/np.float32(2), centers+size/np.float32(2)),axis=1)
    keypoints = raw[:,4:].reshape(-1,4,2)/np.float32(224)+ANCHORS[selected,None,:]
    return tuple(Detection(float(scores[index]),tuple(map(float,box)),
                  tuple(tuple(map(float,p))for p in kp))
                 for index,box,kp in zip(selected,boxes,keypoints))


def weighted_nms(detections, *, max_poses=1):
    """Official weighted-IoU clusters, strict IoU>.5; top seed keeps its score.

    One-pose mode stops after the first complete cluster; later clusters cannot
    change that result (the official graph clips its final vector to num_poses).
    """
    remaining = sorted(detections,key=lambda d:d.score,reverse=True)
    output=[]
    while remaining and len(output)<max_poses:
        seed=remaining[0]
        boxes=np.asarray([d.box_xyxy for d in remaining],np.float32)
        ref=np.asarray(seed.box_xyxy,np.float32)
        wh=np.maximum(np.float32(0),np.minimum(boxes[:,2:],ref[2:])-np.maximum(boxes[:,:2],ref[:2]))
        intersection=wh[:,0]*wh[:,1]
        area=(boxes[:,2]-boxes[:,0])*(boxes[:,3]-boxes[:,1])
        refarea=(ref[2]-ref[0])*(ref[3]-ref[1])
        union=area+refarea-intersection
        iou=np.divide(intersection,union,out=np.zeros_like(union),where=union>0)
        group=iou>np.float32(.5)
        candidates=[d for d,include in zip(remaining,group)if include]
        if candidates:
            score=np.asarray([d.score for d in candidates],np.float32)
            total=score.sum(dtype=np.float32)
            bbox=(np.asarray([d.box_xyxy for d in candidates],np.float32)*score[:,None]).sum(axis=0,dtype=np.float32)/total
            kp=(np.asarray([d.keypoints4 for d in candidates],np.float32)*score[:,None,None]).sum(axis=0,dtype=np.float32)/total
            output.append(Detection(seed.score,tuple(map(float,bbox)),tuple(tuple(map(float,p))for p in kp)))
            remaining=[d for d,include in zip(remaining,group)if not include]
        else:
            # Official zero-area case emits seed and stops if no progress.
            output.append(seed)
            break
    return tuple(output)


def remove_letterbox(detection,padding):
    left,top,right,bottom=map(np.float32,padding)
    scale=np.array([np.float32(1)-(left+right),np.float32(1)-(top+bottom)],np.float32)
    offset=np.array([left,top],np.float32)
    box=(np.asarray(detection.box_xyxy,np.float32).reshape(2,2)-offset)/scale
    kp=(np.asarray(detection.keypoints4,np.float32)-offset)/scale
    return Detection(detection.score,tuple(map(float,box.flat)),tuple(tuple(map(float,p))for p in kp))


def alignment_roi(keypoints, width, height):
    center,scale=keypoints[:2]
    dx=(scale[0]-center[0])*width;dy=(scale[1]-center[1])*height
    side=2*math.hypot(dx,dy)*1.25
    angle=math.pi/2-math.atan2(-dy,dx)
    angle-=2*math.pi*math.floor((angle+math.pi)/(2*math.pi))
    return (center[0],center[1],side/width,side/height,angle)


def crop_rgb(rgb, rect, size, *, keep_aspect, border_mode):
    """Official OpenCV converter: PadRoi/boxPoints/warpPerspective, RGB stays U8.

    Returns original NORM_RECT plus actual tensor letterbox; padded physical ROI
    is used only for sampling, exactly as ImageToTensorCalculator's contract.
    """
    h,w=rgb.shape[:2]
    cx,cy,rw,rh,angle=map(np.float32,rect)
    pw,ph=np.float32(rw*w),np.float32(rh*h)
    left=top=np.float32(0)
    if keep_aspect:
        ratio=np.float32(ph/pw)
        if ratio<1:
            top=(np.float32(1)-ratio)/np.float32(2);ph=pw
        else:
            left=(np.float32(1)-np.float32(1)/ratio)/np.float32(2);pw=ph
    degrees=np.float32(float(np.float32(angle*np.float32(180)))/math.pi)
    source=cv2.boxPoints(((float(cx*w),float(cy*h)),(float(pw),float(ph)),float(degrees)))
    destination=np.array([[0,size],[0,0],[size,0],[size,size]],np.float32)
    matrix=cv2.getPerspectiveTransform(source,destination)
    image=cv2.warpPerspective(rgb,matrix,(size,size),flags=cv2.INTER_LINEAR,
                              borderMode=border_mode,borderValue=(0,0,0))
    return image,FrameRoi(tuple(rect),(float(left),float(top),float(left),float(top)))


class PoseDetector:
    """Existing board LiteRT, allocated once; injectable for host formula tests."""
    def __init__(self, model_path=None, *, interpreter=None):
        if interpreter is None:
            from ai_edge_litert.interpreter import Interpreter,OpResolverType
            interpreter=Interpreter(model_path=str(model_path),num_threads=1,
                experimental_op_resolver_type=OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES)
        self.interpreter=interpreter
        interpreter.allocate_tensors()
        inp=interpreter.get_input_details();out=interpreter.get_output_details()
        if len(inp)!=1 or tuple(inp[0]['shape'])!=(1,224,224,3) or np.dtype(inp[0]['dtype'])!=np.dtype('float32'):
            raise ValueError('original detector input must be float32[1,224,224,3]')
        self.input_index=int(inp[0]['index'])
        shapes={tuple(d['shape']):int(d['index'])for d in out if np.dtype(d['dtype'])==np.dtype('float32')}
        if set(shapes)!={(1,2254,12),(1,2254,1)}:
            raise ValueError('original detector outputs must be boxes2254x12 and scores2254x1')
        self.box_index,self.score_index=shapes[(1,2254,12)],shapes[(1,2254,1)]
        self.calls=0

    def detect(self,rgb):
        cropped,roi=crop_rgb(rgb,(.5,.5,1.,1.,0.),224,keep_aspect=True,border_mode=cv2.BORDER_CONSTANT)
        tensor=cropped.astype(np.float32)*np.float32(2/255)-np.float32(1)
        self.interpreter.set_tensor(self.input_index,tensor[None])
        self.interpreter.invoke();self.calls+=1
        found=weighted_nms(decode_detections(self.interpreter.get_tensor(self.box_index),
                                           self.interpreter.get_tensor(self.score_index)))
        return tuple(remove_letterbox(d,roi.padding)for d in found)


class LiveRoiFrontend:
    """One physical-view owner, one outstanding source; success updates next ROI."""
    def __init__(self,detector):
        self.detector=detector
        self.reset()

    def reset(self):
        self.rect=self.previous=self.pending=None
        self.epoch=None

    def prepare(self,source,rgb,*,epoch=0):
        if self.pending is not None:
            raise ValueError('finish or cancel current source before preparing next image')
        if (rgb.dtype!=np.uint8 or rgb.shape!=(source.height,source.width,3)):
            raise ValueError('RGB bytes must match oriented source dimensions')
        previous=self.previous
        if previous is not None:
            if epoch==self.epoch and source.timestamp_ns<previous.timestamp_ns:
                raise ValueError('source timestamp moved backwards; reset explicitly')
            if ((source.view,source.width,source.height,source.rotation_degrees)!=(previous.view,previous.width,previous.height,previous.rotation_degrees)
                    or source.timestamp_ns-previous.timestamp_ns>250_000_000 or epoch!=self.epoch):
                self.rect=None
        self.previous,self.epoch=source,epoch
        if self.rect is None:
            detections=self.detector.detect(rgb)
            if not detections:
                return None
            self.rect=alignment_roi(detections[0].keypoints4,source.width,source.height)
            if min(self.rect[2:4])<=0:
                self.rect=None
                return None
        # Original landmark preprocessing has default stretch/no letterbox and
        # BORDER_REPLICATE. Physical detector/tracked ROI is already square.
        cropped,roi=crop_rgb(rgb,self.rect,256,keep_aspect=False,border_mode=cv2.BORDER_REPLICATE)
        q7=cv2.LUT(cropped,Q7_RGB_LUT).tobytes()
        self.pending=PreparedImage(source,roi,q7)
        return self.pending

    def update(self,decoded):
        if self.pending is None or decoded.source!=self.pending.source:
            raise ValueError('decoded result must own the pending original source')
        rect=decoded.next_roi()
        self.rect=rect if rect is not None and min(rect[2:4])>0 else None
        self.pending=None

    def finish(self,heads,heat_q2):
        """Complete the pending source with combined-worker raw S16/heat bytes."""
        if self.pending is None:
            raise ValueError('no pending image for tail output')
        result=decode(self.pending.source,self.pending.roi,heads,heat_q2)
        self.update(result)
        return result

    def cancel(self):
        self.pending=None
        self.rect=None
