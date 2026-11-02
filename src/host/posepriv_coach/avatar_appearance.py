"""Local visible-appearance extraction; no identification or inferred hidden surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
from .avatar_texture import AvatarTexture


@dataclass(frozen=True, slots=True)
class AvatarAppearance:
    shirt_rgb: tuple
    trousers_rgb: tuple
    shoes_rgb: tuple
    skin_rgb: tuple
    hair_rgb: tuple
    long_sleeves: bool
    shoulder_scale: float
    waist_scale: float
    arm_scale: float
    leg_scale: float
    head_scale: float
    texture: AvatarTexture

    def __post_init__(self):
        for name in ('shirt_rgb','trousers_rgb','shoes_rgb','skin_rgb','hair_rgb'):
            color = tuple(getattr(self, name))
            if len(color) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in color):
                raise ValueError("appearance colors must be RGB8")
            object.__setattr__(self, name, color)
        if type(self.long_sleeves) is not bool or type(self.texture) is not AvatarTexture:
            raise ValueError("appearance requires sleeve flag and immutable photo atlas")
        if (self.texture.width,self.texture.height) != (256,512):
            raise ValueError("appearance atlas must be 256x512 RGB8")
        for name in ('shoulder_scale','waist_scale','arm_scale','leg_scale','head_scale'):
            v = getattr(self,name)
            if not isinstance(v,(int,float)) or not math.isfinite(v) or not .25 <= v <= 2.:
                raise ValueError("appearance proportions must be finite in .25..2")

    def to_mapping(self):
        data = {name:getattr(self,name) for name in self.__dataclass_fields__ if name != 'texture'}
        return {"schema_version":1, **data, "texture":self.texture.to_mapping(),
                "scope":"visible photographic appearance; not a calibrated full-body scan"}

    @classmethod
    def from_mapping(cls,data):
        if data.get('schema_version') != 1: raise ValueError('unsupported appearance schema')
        fields = {name:data[name] for name in cls.__dataclass_fields__ if name != 'texture'}
        return cls(**fields, texture=AvatarTexture.from_mapping(data['texture']))


def load_avatar_appearance(path):
    return AvatarAppearance.from_mapping(json.loads(Path(path).read_text(encoding='utf-8')))


def estimate_avatar_appearance(front_image, front, side_image, side):
    """Build a photographic atlas from one synchronized, well-observed reference pair."""
    import cv2
    import numpy as np
    from .skeleton_pose import DEFAULT_BONE_LINKS
    def points(image, pose):
        if image.shape[:2] != (pose.source_height,pose.source_width):
            raise ValueError('reference image geometry does not match pose')
        result = {n:np.array((p.x*pose.source_width,p.y*pose.source_height),float)
                  for n,p in pose.keypoints.items() if p.confidence>=.5 and math.isfinite(p.x)
                  and math.isfinite(p.y) and 0<=p.x<=1 and 0<=p.y<=1}
        for name,a,b in (('neck','left_shoulder','right_shoulder'),('pelvis','left_hip','right_hip')):
            if a not in result or b not in result:raise ValueError('reference shoulders and hips must be visible')
            result[name]=(result[a]+result[b])/2
        result['chest']=result['neck']*.7+result['pelvis']*.3
        return result
    fp,sp=points(front_image,front),points(side_image,side)
    span=abs(fp['left_shoulder'][0]-fp['right_shoulder'][0])
    if span<20:raise ValueError('reference person is too small')
    def foreground(image,pts):
        mask=np.zeros(image.shape[:2],np.uint8)
        all_points=np.array(list(pts.values()))
        lo=all_points.min(0)-np.array((span*.35,span*.6))
        hi=all_points.max(0)+np.array((span*.35,span*.4))
        x0,y0=np.maximum(0,np.floor(lo).astype(int));x1,y1=np.minimum((image.shape[1],image.shape[0]),np.ceil(hi).astype(int))
        mask[y0:y1,x0:x1]=cv2.GC_PR_FGD
        torso=np.int32([pts[n] for n in ('left_shoulder','right_shoulder','right_hip','left_hip')])
        core=np.zeros_like(mask);cv2.fillConvexPoly(core,torso,1)
        core=cv2.erode(core,np.ones((15,15),np.uint8));mask[core>0]=cv2.GC_FGD
        for link in DEFAULT_BONE_LINKS:
            if link.parent in pts and link.child in pts:
                cv2.line(mask,tuple(np.rint(pts[link.parent]).astype(int)),tuple(np.rint(pts[link.child]).astype(int)),cv2.GC_FGD,max(3,round(span*.025)))
        cv2.grabCut(image,mask,None,np.zeros((1,65)),np.zeros((1,65)),3,cv2.GC_INIT_WITH_MASK)
        return np.uint8((mask==cv2.GC_FGD)|(mask==cv2.GC_PR_FGD))*255
    masks={id(front_image):foreground(front_image,fp),id(side_image):foreground(side_image,sp)}
    def strip(image,start,end,halfwidth):
        direction=end-start; length=float(np.linalg.norm(direction))
        if length<3:raise ValueError('reference bone too short')
        normal=np.array((direction[1],-direction[0]))/length
        if normal[0]<0:normal=-normal
        source=np.float32([start-normal*halfwidth,start+normal*halfwidth,
                           end+normal*halfwidth,end-normal*halfwidth])
        target=np.float32([[0,0],[63,0],[63,127],[0,127]])
        transform=cv2.getPerspectiveTransform(source,target)
        patch=cv2.warpPerspective(image,transform,(64,128),borderMode=cv2.BORDER_REPLICATE)
        mask=cv2.warpPerspective(masks[id(image)],transform,(64,128),flags=cv2.INTER_NEAREST)
        if np.any(mask>0):
            patch=cv2.inpaint(patch,np.uint8(mask==0)*255,3,cv2.INPAINT_TELEA)
        return patch
    atlas=np.zeros((512,256,3),np.uint8)
    def put(tile,image):
        x,y=tile%4*64,tile//4*128;atlas[y:y+128,x:x+64]=image
    patches=[]
    for i,link in enumerate(DEFAULT_BONE_LINKS):
        if link.parent not in fp or link.child not in fp:
            patch=np.full((128,64,3),128,np.uint8)
        else:
            width=span*(.48 if i==0 else .16 if i==1 else .08 if 'arm' in link.name else .12)
            start,end=fp[link.parent],fp[link.child]
            if i==0:end=fp['neck']
            if i==1:
                start=fp['neck'];end=fp.get('nose',fp['neck']-np.array((0.,span*.4)))+np.array((0.,span*.25))
                width=span*.08
            patch=strip(front_image,start,end,width)
        patches.append(patch);put(i,patch)
    def face_patch(image,pts,profile=False):
        nose=pts.get('nose',pts['neck']-np.array((0.,span*.4)))
        box=None
        cascade_path=Path(getattr(cv2,'data',object()).haarcascades)/'haarcascade_frontalface_default.xml' if hasattr(getattr(cv2,'data',None),'haarcascades') else None
        if not profile and cascade_path is not None and cascade_path.is_file():
            detector=cv2.CascadeClassifier(str(cascade_path))
            boxes=detector.detectMultiScale(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),1.1,4,minSize=(30,30))
            if len(boxes):
                box=min(boxes,key=lambda r:abs(r[0]+r[2]/2-nose[0])+abs(r[1]+r[3]/2-nose[1]))
        if box is not None:
            x,y,w,h=map(float,box);top=np.array((x+w/2,y-h*.22));bottom=np.array((x+w/2,y+h*1.08));half=w*.60
        else:
            top=nose-np.array((0.,span*.45));bottom=nose+np.array((0.,span*.27));half=span*.25
        return cv2.flip(strip(image,top,bottom,half),0),half*2
    head,head_width=face_patch(front_image,fp);side_head,_=face_patch(side_image,sp,True)
    put(2,head);put(13,side_head)
    side_body=strip(side_image,sp['pelvis'],sp['neck'],span*.18);put(12,side_body)
    # Hands and shoes have their own visible-reference patches, not the sleeve texture.
    for name,tile in (('left',11),('right',14)):
        wrist=fp.get(name+'_wrist',fp[name+'_hip']);elbow=fp.get(name+'_elbow',fp[name+'_shoulder'])
        hand=strip(front_image,wrist,wrist+(wrist-elbow)*.3,span*.055);put(tile,hand)
    ankle=fp.get('left_ankle',fp['pelvis']+np.array((0.,span*2)))
    shoe=strip(front_image,ankle,ankle+np.array((0.,span*.28)),span*.13);put(15,shoe)
    def rgb(image):return tuple(int(v) for v in np.median(image.reshape(-1,3),axis=0)[::-1])
    shirt=rgb(patches[0][20:108,16:48]);skin=rgb(head[35:70,24:40]);hair_pixels=head[98:128].reshape(-1,3)
    hair_pixels=hair_pixels[np.sum(hair_pixels,axis=1)<=np.percentile(np.sum(hair_pixels,axis=1),35)]
    hair=tuple(int(v) for v in np.median(hair_pixels,axis=0)[::-1])
    sleeve=rgb(patches[4][25:100,20:44]);long_sleeves=np.linalg.norm(np.array(sleeve)-shirt)<np.linalg.norm(np.array(sleeve)-skin)
    image_scale=2/max(front.source_width,front.source_height)
    def bounded(v,low=.4,high=1.5):return float(np.clip(v,low,high))
    # Limb widths are visible garment silhouettes; these are display proportions, not measurements.
    def limb_scale(parent,child,nominal):
        if parent not in fp or child not in fp:return .7
        center=(fp[parent]+fp[child])/2;delta=fp[child]-fp[parent];normal=np.array((delta[1],-delta[0]))/max(1,np.linalg.norm(delta))
        cx,cy=np.rint(center).astype(int);cx=int(np.clip(cx,0,front.source_width-1));cy=int(np.clip(cy,0,front.source_height-1))
        color=front_image[cy,cx].astype(float);extent=[]
        for sign in (-1,1):
            distance=0
            for k in range(1,max(3,int(span*.3))):
                x,y=np.rint(center+normal*k*sign).astype(int)
                if not(0<=x<front.source_width and 0<=y<front.source_height):break
                if np.linalg.norm(front_image[y,x].astype(float)-color)>55:break
                distance=k
            extent.append(distance)
        return bounded(sum(extent)*image_scale/nominal,.35,1.3)
    return AvatarAppearance(shirt,rgb(patches[7]),rgb(shoe),skin,hair,bool(long_sleeves),
        bounded(span*image_scale*1.08/.336,.65,1.4),
        bounded(abs(fp['left_hip'][0]-fp['right_hip'][0])*image_scale*1.35/.273,.65,1.4),
        limb_scale('left_shoulder','left_elbow',.182),limb_scale('left_hip','left_knee',.252),
        bounded(head_width*image_scale/.2156,.55,1.3),AvatarTexture(256,512,atlas[:,:,::-1].copy().tobytes()))
