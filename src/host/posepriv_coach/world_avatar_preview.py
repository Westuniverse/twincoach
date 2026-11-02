"""Explicit image-only world-pose display; no observed stereo pose or scoring.

Fixed rig proportions remove learned bone-length/torso-scale pumping. Optional
reviewed image framing retains image-plane pelvis motion; camera-depth root
translation and ground contact are not estimated.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from statistics import median
from types import MappingProxyType, SimpleNamespace

from .avatar_image_source import AvatarImageSource, ImageAvatarFramePlan
from .avatar_skinning import AffineMatrixQ1_14, MatrixWrite
from .preview_current_body_rotation import PreviewCurrentBodyRotation
from .preview_head_orientation import (CurrentHeadOrientation, PreviewHeadOrientation,
    world_body_rotation, world_face_plane_rotation)
from .preview_rigid_avatar import PreviewRigidAvatar, _matmul, _norm
from .skeleton_pose import SkeletonJoint3D, build_virtual_joints, build_bone_segments

WORLD_JOINTS = {0:'nose', 2:'left_eye', 5:'right_eye', 7:'left_ear', 8:'right_ear',
    11:'left_shoulder', 12:'right_shoulder', 13:'left_elbow', 14:'right_elbow',
    15:'left_wrist', 16:'right_wrist', 23:'left_hip', 24:'right_hip',
    25:'left_knee', 26:'right_knee', 27:'left_ankle', 28:'right_ankle'}


@dataclass(frozen=True, slots=True)
class WorldDisplayObservation:
    source: AvatarImageSource
    world_landmarks: tuple
    normalized_landmarks: tuple

    def __post_init__(self):
        if (not isinstance(self.source, AvatarImageSource) or
                len(self.world_landmarks) not in (0,33) or
                len(self.normalized_landmarks) != len(self.world_landmarks)):
            raise ValueError('one image and zero or 33 corresponding model landmarks required')
        for field in ('world_landmarks','normalized_landmarks'):
            points=[]
            for p in getattr(self,field):
                if (not all(math.isfinite(p[k])for k in ('x','y','z','presence','visibility')) or
                        not 0<=p['presence']<=1 or not 0<=p['visibility']<=1):
                    raise ValueError('finite model coordinates and separate confidences required')
                points.append(MappingProxyType(dict(p)))
            object.__setattr__(self,field,tuple(points))


def world_observation_from_reference(record,source):
    """Bind exported coordinates only to their exact reviewed raw image."""
    if (record.get('schema_version')!=1 or
            record.get('scope')!='offline_world_pose_reference' or
            (record['view'],record['source_frame_id'],record['source_timestamp_ns'],
             record['rotation_degrees'])!=(source.view,source.frame_id,source.timestamp_ns,
                                           source.rotation_degrees) or
            'image_width'in record and (record['image_width'],record['image_height'])!=(
                source.width,source.height)):
        raise ValueError('world reference and original image source differ')
    world=record['world_landmarks'];image=record['normalized_landmarks']
    if len(world)!=len(image)or len(world)>1:raise ValueError('single-person world reference required')
    return WorldDisplayObservation(source,tuple(world[0])if world else (),
                                   tuple(image[0])if image else ())


@dataclass(frozen=True, slots=True)
class WorldDisplayFraming:
    """Fixed image-plane reference from an explicitly reviewed standing window."""
    view: str
    width: int
    height: int
    rotation_degrees: int
    hip_xy: tuple[float,float]
    model_units_per_image_height: float

    def __post_init__(self):
        if (self.view not in ('front','side') or min(self.width,self.height)<=0 or
                self.rotation_degrees not in (0,90,180,270) or len(self.hip_xy)!=2 or
                not all(math.isfinite(v)for v in self.hip_xy) or
                not math.isfinite(self.model_units_per_image_height) or
                self.model_units_per_image_height<=0):
            raise ValueError('fixed reviewed image framing required')

    def offset(self,observation):
        source=observation.source
        if (source.view,source.width,source.height,source.rotation_degrees)!=(
                self.view,self.width,self.height,self.rotation_degrees):
            raise ValueError('framing and original image geometry differ')
        hip=_image_hip(observation)
        unit=self.model_units_per_image_height
        return ((hip[0]-self.hip_xy[0])*source.width/source.height*unit,
                -(hip[1]-self.hip_xy[1])*unit,0.)


def _image_hip(observation):
    points=observation.normalized_landmarks
    if not points or any(points[i]['presence']<.5 for i in (23,24)):
        raise ValueError('current image hip unavailable')
    return tuple((points[23][k]+points[24][k])/2 for k in ('x','y'))


def fit_world_display_framing(observations,torso_length):
    """Median of caller-selected standing images; duplicates count only once."""
    import numpy as np
    sources=set();hips=[];units=[];geometry=None
    if not math.isfinite(torso_length) or torso_length<=0:
        raise ValueError('positive bound torso length required')
    for observation in observations:
        s=observation.source;g=(s.view,s.width,s.height,s.rotation_degrees)
        if geometry is not None and geometry!=g:raise ValueError('mixed framing cameras')
        geometry=g
        key=(s.frame_id,s.timestamp_ns)
        if key in sources:continue
        sources.add(key);p=observation.normalized_landmarks
        if not p or any(min(p[i]['presence'],p[i]['visibility'])<.5 for i in (11,12,23,24)):
            continue
        if any(not 0<=p[i][k]<=1 for i in (11,12,23,24)for k in ('x','y')):
            continue
        # This fixed close-up demonstration reference needs a sufficiently
        # large person. In the raw negative clips the tracked background person
        # occupies <.22 image heights. This is framing eligibility, not identity
        # recognition or a universal tracking threshold.
        visible=[p[i]for i in (0,11,12,23,24,25,26,27,28)if p[i]['presence']>=.5]
        extent=max(max(q['y']for q in visible)-min(q['y']for q in visible),
            s.width/s.height*(max(q['x']for q in visible)-min(q['x']for q in visible)))
        if extent<.25:continue
        hip=_image_hip(observation)
        shoulder=tuple((p[11][k]+p[12][k])/2 for k in ('x','y'))
        distance=math.hypot((shoulder[0]-hip[0])*s.width/s.height,shoulder[1]-hip[1])
        if distance<.02:continue
        hips.append(hip);units.append(torso_length/distance)
    if len(hips)<3:raise ValueError('at least three usable standing source images required')
    return WorldDisplayFraming(*geometry,tuple(np.median(hips,axis=0)),float(np.median(units)))


@dataclass(frozen=True, slots=True)
class WorldUprightDisplayReference:
    """Reviewed standing model direction, not extrinsics or measured gravity.

    A fixed minimal rotation aligns this direction with display +Y. Apply to the
    complete avatar around its current displayed pelvis, preserving root framing.
    """
    view: str
    width: int
    height: int
    rotation_degrees: int
    up_direction: tuple[float,float,float]
    source_count: int

    def __post_init__(self):
        up=tuple(self.up_direction)
        if (self.view not in ('front','side') or min(self.width,self.height)<=0 or
                self.rotation_degrees not in (0,90,180,270) or len(up)!=3 or
                not all(math.isfinite(v)for v in up) or up[1]<=0 or
                abs(math.sqrt(math.fsum(v*v for v in up))-1)>1e-6 or
                type(self.source_count)is not int or self.source_count<3):
            raise ValueError('reviewed standing unit direction and image geometry required')
        length=math.sqrt(math.fsum(v*v for v in up))
        object.__setattr__(self,'up_direction',tuple(v/length for v in up))

    def rotation(self):
        """Rodrigues minimal rotation; up.Y>0 excludes half-turn ambiguity."""
        import numpy as np
        x,y,z=self.up_direction
        a,b,c=-z,0.,x  # cross(source up, display +Y)
        cross=np.array(((0.,-c,b),(c,0.,-a),(-b,a,0.)))
        return np.eye(3)+cross+_matmul(cross,cross)/(1+y)

    def apply(self,preview,source):
        import numpy as np
        if (source.view,source.width,source.height,source.rotation_degrees)!=(
                self.view,self.width,self.height,self.rotation_degrees):
            raise ValueError('upright reference and original image geometry differ')
        # Canonical reference poses are authored upright, outside the model's
        # source camera frame. Do not rotate them as if currently detected.
        if preview.reason not in ('tracked','partial','held_body','recorded_hold'):
            return preview
        display=np.array(preview.display_transform).reshape(4,4)
        pelvis=np.array(preview.render_plan.joints['pelvis'].position)
        pivot=_matmul(display[:3,:3],pelvis)+display[:3,3]
        rotation=self.rotation()
        result=display.copy()
        result[:3,:3]=_matmul(rotation,display[:3,:3])
        result[:3,3]=pivot+_matmul(rotation,display[:3,3]-pivot)
        return replace(preview,display_transform=tuple(result.flat))


def fit_world_upright_display_reference(observations):
    """Only an explicit reviewed upright window; repeats cannot reweight a fit."""
    import numpy as np
    geometry=None;seen={};frame_times={};directions=[]
    for observation in observations:
        source=observation.source
        current=(source.view,source.width,source.height,source.rotation_degrees)
        if geometry is not None and current!=geometry:
            raise ValueError('mixed upright reference cameras')
        geometry=current;key=(source.frame_id,source.timestamp_ns)
        if source.frame_id in frame_times and frame_times[source.frame_id]!=source.timestamp_ns:
            raise ValueError('same upright image has changed source clock')
        frame_times[source.frame_id]=source.timestamp_ns
        payload=(observation.world_landmarks,observation.normalized_landmarks)
        if key in seen:
            if seen[key]!=payload:raise ValueError('same upright image has changed coordinates')
            continue
        seen[key]=payload
        world,image=payload
        indices=(11,12,23,24)
        if (not world or any(min(world[i]['presence'],world[i]['visibility'],
                image[i]['presence'],image[i]['visibility'])<.5 for i in indices) or
                any(not 0<=image[i][k]<=1 for i in indices for k in ('x','y'))):
            continue
        up=np.array([(world[11][k]+world[12][k]-world[23][k]-world[24][k])/2
                     for k in ('x','y','z')])*np.array((1.,-1.,1.))
        length=_norm(up)
        if length<=1e-7 or up[1]<=0:continue
        directions.append(up/length)
    if len(directions)<3:raise ValueError('at least three usable standing source images required')
    direction=np.median(directions,axis=0);direction/=_norm(direction)
    return WorldUprightDisplayReference(*geometry,tuple(direction),len(directions))


@dataclass(frozen=True, slots=True)
class WorldHeadPitchReference:
    """Fixed eye/mouth-plane pitch from caller-reviewed neutral head images.

    This is a display reference, not camera calibration or measured head pose.
    Right multiplication removes the plane's local bias without rotating the
    source camera axes or subtracting genuine changes in yaw, pitch or roll.
    """
    view: str
    width: int
    height: int
    rotation_degrees: int
    pitch_radians: float
    source_count: int

    def __post_init__(self):
        if (self.view not in ('front','side') or min(self.width,self.height)<=0 or
                self.rotation_degrees not in (0,90,180,270) or
                not math.isfinite(self.pitch_radians) or
                not -math.pi/2<self.pitch_radians<math.pi/2 or
                type(self.source_count)is not int or self.source_count<3):
            raise ValueError('reviewed neutral head pitch and image geometry required')

    def rotation(self,observation):
        import numpy as np
        s=observation.source
        if (s.view,s.width,s.height,s.rotation_degrees)!=(
                self.view,self.width,self.height,self.rotation_degrees):
            raise ValueError('head reference and original image geometry differ')
        raw=np.array(world_face_plane_rotation(observation.world_landmarks)).reshape(3,3)
        body=np.array(world_body_rotation(observation.world_landmarks)).reshape(3,3)
        if sum(float(a*b)for a,b in zip(raw[:,2],body[:,2]))<=0:
            raise ValueError('original head plane conflicts with source body')
        c,s=math.cos(self.pitch_radians),math.sin(self.pitch_radians)
        inverse=np.array(((1.,0.,0.),(0.,c,s),(0.,-s,c)))
        return tuple(_matmul(raw,inverse).flat)


@dataclass(frozen=True, slots=True)
class WorldCameraHeadPitchReference(WorldHeadPitchReference):
    """Reviewed camera-level neutral head, without rotating the body or limbs.

    The inherited fixed local inverse pitch is display-only. This distinct type
    denotes a source-camera +Y reference rather than the body's learned up axis.
    """


def _fit_world_head_pitch_reference(observations, *, camera_level=False):
    import numpy as np
    geometry=None;sources=set();pitches=[]
    for observation in observations:
        s=observation.source;g=(s.view,s.width,s.height,s.rotation_degrees)
        if geometry is not None and geometry!=g:raise ValueError('mixed head reference cameras')
        geometry=g;key=(s.frame_id,s.timestamp_ns)
        if key in sources:continue
        sources.add(key)
        p=observation.world_landmarks
        if not p or any(min(p[i]['presence'],p[i]['visibility'])<.5
                        for i in (2,5,9,10,11,12,23,24)):
            continue
        try:
            head=np.array(world_face_plane_rotation(p)).reshape(3,3)
            body=np.array(world_body_rotation(p)).reshape(3,3)
        except ValueError:continue
        if camera_level:
            if head[1,1]<=0 or head[2,2]<=0:continue
            pitches.append(math.atan2(-float(head[1,2]),float(head[1,1])))
        else:
            relative=_matmul(body.T,head)
            if relative[2,2]<=0:continue
            pitches.append(math.atan2(float(relative[2,1]),float(relative[1,1])))
    if len(pitches)<3:raise ValueError('at least three visible neutral head source images required')
    reference=WorldCameraHeadPitchReference if camera_level else WorldHeadPitchReference
    return reference(*geometry,median(pitches),len(pitches))


def fit_world_head_pitch_reference(observations):
    """Fit reviewed body-relative neutral pitch, counting each image once."""
    return _fit_world_head_pitch_reference(observations)


def fit_world_camera_head_pitch_reference(observations):
    """Fit explicitly reviewed camera-level neutral head; no body correction."""
    return _fit_world_head_pitch_reference(observations,camera_level=True)


class _WorldHeading:
    def reset(self):pass

    def apply(self,observed,render,*,display_timestamp_ns):
        left=observed.joints.get('left_shoulder');right=observed.joints.get('right_shoulder')
        yaw=None
        if left and right and left.valid and right.valid:
            delta=tuple(a-b for a,b in zip(left.position,right.position))
            if math.hypot(delta[0],delta[2])>1e-7:yaw=math.atan2(delta[2],delta[0])
        return SimpleNamespace(yaw_radians=yaw)


class _WorldRig(PreviewRigidAvatar):
    """Reuse fixed geometry/palm continuity, with explicit single-image clock."""
    def __init__(self,*args,full_body_basis=False,**kwargs):
        super().__init__(*args,**kwargs)
        self.heading=_WorldHeading()
        self.full_body_basis=full_body_basis

    def _body_rotation(self,observed,yaw,pelvis,neck):
        if not self.full_body_basis:
            return super()._body_rotation(observed,yaw,pelvis,neck)
        lateral=self._point(observed.joints,'left_shoulder')-self._point(
            observed.joints,'right_shoulder')
        return _matmul(self._basis(lateral,neck-pelvis),self.base.T)

    def _source_clock(self,observed,render):
        if observed.source!=render.source:raise ValueError('image rig sources differ')
        return observed.source.timestamp_ns,observed.source.timestamp_ns

    def _supported_depth(self,observed,side,supported):
        return False  # Model depth is inferred, never stereo-supported depth.

    def _held_render_plan(self,previous,render):
        return replace(previous,source=render.source)

    def _ik(self,start,hint,target,l1,l2):
        middle=start+l1*self._unit(hint-start)
        return middle,middle+l2*self._unit(target-hint)

    def _head_up(self,observed,rotation,display_ns,dt):
        return self._unit(_matmul(rotation,self.bind[self.slot['head']][:3,1]))

    def _head_reference(self,observed,rotation,direction,display_ns,dt):
        return self._unit(_matmul(rotation,self.bind[self.slot['head']][:3,0])),'world_body_prior',0.


class WorldAvatarPreview:
    """Opt-in host reference. A new instance/reset is required per chapter.

    All13 slots stay inferred, even when model presence/visibility are high.
    Framing=None explicitly means in-place animation; it cannot preserve root
    translation. Provide reviewed framing to retain image-plane body movement.
    """
    def __init__(self,asset,bone_links,topology_indices,*,proportions=None,framing=None,
                 vertex_requests=(),head_maximum_rate=4.,head_reference=None,
                 full_body_basis=False,upright_reference=None,inferred_metadata_builder=None,
                 initial_plan_builder=None):
        if type(full_body_basis)is not bool:raise ValueError('explicit full body basis boolean required')
        if inferred_metadata_builder is not None and not callable(inferred_metadata_builder):
            raise TypeError('inferred display metadata builder must be callable')
        self.rig=_WorldRig(asset,bone_links,topology_indices,proportions=proportions,
                           full_body_basis=full_body_basis)
        self.head=PreviewHeadOrientation(asset,bone_links,topology_indices,
                                         maximum_rate=head_maximum_rate)
        self.body=PreviewCurrentBodyRotation(asset.bone_names.index('spine'))
        self.framing=framing;self.requests=tuple(vertex_requests)
        self.head_reference=head_reference
        self.upright_reference=upright_reference
        self.inferred_metadata_builder=inferred_metadata_builder
        if initial_plan_builder is not None and not callable(initial_plan_builder):
            raise TypeError('initial world-plan builder must be callable')
        self.initial_plan_builder=initial_plan_builder
        self.reset()

    def reset(self):
        self.rig.reset();self.head.reset();self.body.reset()
        self._source=None;self._display_ns=None;self._cached=None

    def _initial_plan(self,observation):
        """Pure-Python reference for the optional native materialization boundary."""
        import numpy as np
        s=observation.source
        joints={}
        for i,name in WORLD_JOINTS.items()if observation.world_landmarks else ():
            p=observation.world_landmarks[i]
            joints[name]=SkeletonJoint3D(name,(p['x'],-p['y'],p['z']),
                min(p['presence'],p['visibility']),math.inf,p['presence']>=.5)
        joints=build_virtual_joints(joints)
        if all(n in joints and joints[n].valid for n in ('pelvis','neck')):
            origin=np.array(joints['pelvis'].position);vector=np.array(joints['neck'].position)-origin
            length=_norm(vector)
            if length>1e-7:
                offset=self.framing.offset(observation)if self.framing else (0.,0.,0.)
                root=self.rig.origin+offset;factor=self.rig.torso_length/length
                joints={n:replace(j,position=tuple(root+(np.array(j.position)-origin)*factor))
                        for n,j in joints.items()}
        segments=build_bone_segments(joints,self.rig.links)
        valid=tuple(segments[k].valid for k in self.rig.indices)+(False,)*3
        plan=ImageAvatarFramePlan(s,joints,segments,
            tuple(MatrixWrite(i,AffineMatrixQ1_14.identity())for i in range(16)),
            valid,self.requests)
        return plan

    def apply(self,observation,*,display_timestamp_ns,recorded_image_hold=False):
        s=observation.source;previous=self._source
        if self.head_reference is not None and (
                s.view,s.width,s.height,s.rotation_degrees)!=(
                self.head_reference.view,self.head_reference.width,self.head_reference.height,
                self.head_reference.rotation_degrees):
            raise ValueError('head reference and original image geometry differ')
        if self.upright_reference is not None and (
                s.view,s.width,s.height,s.rotation_degrees)!=(
                self.upright_reference.view,self.upright_reference.width,
                self.upright_reference.height,self.upright_reference.rotation_degrees):
            raise ValueError('upright reference and original image geometry differ')
        if (self._display_ns is not None and display_timestamp_ns<self._display_ns or
                previous is not None and s.timestamp_ns<previous.timestamp_ns):
            raise ValueError('non-monotonic world display source/time')
        if previous is not None and (s.view,s.width,s.height,s.rotation_degrees)!=(
                previous.view,previous.width,previous.height,previous.rotation_degrees):
            raise ValueError('reset required before changing world display camera')
        age=display_timestamp_ns-s.timestamp_ns
        if age<0:raise ValueError('world display cannot borrow a future image')
        if (s==previous and self._cached is not None and
                (age<=100_000_000 or recorded_image_hold)):
            self._display_ns=display_timestamp_ns
            return replace(self._cached,reason='recorded_hold')if age>100_000_000 else self._cached
        if previous is not None and s.timestamp_ns-previous.timestamp_ns>250_000_000:
            self.rig.reset();self.head.reset();self.body.reset()
        if s!=previous:self._cached=None
        plan=(self._initial_plan(observation) if self.initial_plan_builder is None
              else self.initial_plan_builder(observation))
        result=self.rig.apply(plan,plan,display_timestamp_ns=display_timestamp_ns)
        cues=[]
        if observation.world_landmarks:
            try:
                cues.append(CurrentHeadOrientation(s.frame_id,s.timestamp_ns,'world_face_plane',
                    self.head_reference.rotation(observation)if self.head_reference else
                    world_face_plane_rotation(observation.world_landmarks),
                    world_body_rotation(observation.world_landmarks)))
            except ValueError:pass  # Explicit body prior remains inferred.
        result=self.head.apply(result,cues,display_timestamp_ns=display_timestamp_ns).preview
        result=self.body.apply(result).preview
        if self.upright_reference is not None:
            result=self.upright_reference.apply(result,s)
        inferred=tuple(range(13))
        if self.inferred_metadata_builder is None:
            display_joints={n:replace(j,confidence=0.)for n,j in result.render_plan.joints.items()}
            display_segments=tuple(replace(segment,confidence=0.)for segment in result.render_plan.segments)
        else:
            display_joints,display_segments=self.inferred_metadata_builder(
                result.render_plan.joints,result.render_plan.segments)
        result=replace(result,inferred_slots=inferred,
            reason='partial'if result.reason=='tracked'else result.reason,
            render_plan=replace(result.render_plan,joints=display_joints,segments=display_segments))
        self._source=s;self._display_ns=display_timestamp_ns
        if result.reason=='partial'and age<=100_000_000:self._cached=result
        return result
