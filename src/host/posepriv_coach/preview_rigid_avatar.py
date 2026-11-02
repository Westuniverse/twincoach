"""Opt-in complete display rig; original pose observations and scores are read-only."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from .preview_body_reference import BodyFrame,body_reference_frame,display_affine,IDENTITY_4
import json
from pathlib import Path

from .avatar_skinning import AffineMatrixQ1_14, MatrixWrite, compose_affine_q1_14
from .display_hands import continuous_palm_down_x_axis
from .preview_torso_heading import PreviewTorsoHeading
from .skeleton_pose import (BoneSegment3D, SkeletonJoint3D, bone_affine_matrix_q1_14,
                            build_bone_segments, stable_bone_local_x_axis)

PROPORTION_KEYS=('upper_arm','lower_arm','upper_leg','lower_leg')

def _dot(left,right):
    if len(left)==3:
        return math.fsum((float(left[0])*float(right[0]),float(left[1])*float(right[1]),float(left[2])*float(right[2])))
    return math.fsum(float(a)*float(b)for a,b in zip(left,right))


def _norm(vector):
    return math.sqrt(_dot(vector,vector))


def _cross(left,right):
    import numpy as np
    a,b,c=map(float,left);x,y,z=map(float,right)
    return np.array((b*z-c*y,c*x-a*z,a*y-b*x))


def _matmul(left,right):
    """Fast three-dimensional transforms preserve the fixed scalar fsum contract."""
    import numpy as np
    if left.shape==(3,3)and right.shape==(3,):
        x,y,z=map(float,right)
        return np.array((math.fsum((float(left[0,0])*x,float(left[0,1])*y,float(left[0,2])*z)),
                         math.fsum((float(left[1,0])*x,float(left[1,1])*y,float(left[1,2])*z)),
                         math.fsum((float(left[2,0])*x,float(left[2,1])*y,float(left[2,2])*z))))
    a=np.asarray(left);b=np.asarray(right);vector=b.ndim==1
    if vector:b=b[:,None]
    result=np.array([[_dot(row,b[:,column])for column in range(b.shape[1])]for row in a])
    return result[:,0]if vector else result


def _leg_dot(a, b):
    return math.fsum((a[0]*b[0], a[1]*b[1], a[2]*b[2]))


def _leg_unit(v):
    norm = math.sqrt(_leg_dot(v, v))
    if not math.isfinite(norm) or norm < 1e-7:
        raise ValueError('degenerate rig direction')
    return v[0]/norm, v[1]/norm, v[2]/norm


def _leg_matvec(a, v):
    x, y, z = v
    return (math.fsum((a[0]*x, a[1]*y, a[2]*z)),
            math.fsum((a[3]*x, a[4]*y, a[5]*z)),
            math.fsum((a[6]*x, a[7]*y, a[8]*z)))


def _leg_inverse3(a):
    a,b,c,d,e,f,g,h,i = a
    adj = (e*i-f*h, c*h-b*i, b*f-c*e,
           f*g-d*i, a*i-c*g, c*d-a*f,
           d*h-e*g, b*g-a*h, a*e-b*d)
    determinant = math.fsum((a*adj[0], b*adj[3], c*adj[6]))
    if determinant == 0. or not math.isfinite(determinant):
        raise ValueError('singular display transform')
    return tuple(value/determinant for value in adj)



def _inverse_bind(matrix):
    from fractions import Fraction
    import numpy as np
    rows=[[Fraction(matrix.values[4*r+c],16384)for c in range(4)]for r in range(3)]
    rows.append([Fraction(0),Fraction(0),Fraction(0),Fraction(1)])
    rows=[row+[Fraction(int(r==c))for c in range(4)]for r,row in enumerate(rows)]
    for c in range(4):
        pivot=next(r for r in range(c,4)if rows[r][c])
        rows[c],rows[pivot]=rows[pivot],rows[c]
        divisor=rows[c][c];rows[c]=[v/divisor for v in rows[c]]
        for r in range(4):
            if r==c:continue
            factor=rows[r][c];rows[r]=[a-factor*b for a,b in zip(rows[r],rows[c])]
    return np.array([[float(v)for v in row[4:]]for row in rows])



def validate_proportions(values):
    values=dict(values)
    if set(values)!=set(PROPORTION_KEYS) or any(type(v)not in (int,float) or
            not math.isfinite(v) or not .7<=v<=1.3 for v in values.values()):
        raise ValueError('rig proportions require four finite ratios in [0.7, 1.3]')
    return values


def load_rig_proportions(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value,dict) or type(value.get('schema_version'))is not int or value['schema_version']!=1:
        raise ValueError('unsupported display rig profile schema')
    return validate_proportions(value['proportions'])


def fit_standing_proportions(rig,sequences):
    """Shared-person display proportions from explicit front standing windows.

    The caller selects windows; this is neither action recognition nor physical
    calibration. Side-depth estimates do not influence the fitted lengths.
    """
    np=rig.np;groups={'upper_arm':('shoulder','elbow'),'lower_arm':('elbow','wrist'),
                      'upper_leg':('hip','knee'),'lower_leg':('knee','ankle')}
    profiles=[];counts=[]
    for sequence in sequences:
        samples={name:[]for name in groups};seen=set()
        for plan in sequence:
            stamp=(plan.pair.front.timestamp_ns,plan.pair.side.timestamp_ns)
            if stamp in seen:continue
            seen.add(stamp)
            def point(name):
                joint=plan.joints.get(name)
                if joint is None or not joint.valid or joint.confidence<.5:raise ValueError('weak standing point')
                return np.array(joint.position[:2])
            try:
                torso=point('neck')-point('pelvis');magnitude=float(_norm(torso))
                scale=magnitude/rig.torso_length
                if not .5<scale<1.5 or torso[1]/magnitude<.8:continue
                for side in ('left','right'):
                    a=point(side+'_hip')-point(side+'_knee');b=point(side+'_ankle')-point(side+'_knee')
                    if _norm(a)<1e-7 or _norm(b)<1e-7 or _dot(a,b)/_norm(a)/_norm(b)>-.9:
                        raise ValueError('not a straight standing reference')
            except ValueError:continue
            for name,(a,b)in groups.items():
                for side in ('left','right'):
                    try:
                        value=float(_norm(point(side+'_'+a)-point(side+'_'+b))/scale/rig.length[rig.slot[side+'_'+name]])
                        if math.isfinite(value):samples[name].append(value)
                    except ValueError:continue
        if any(len(values)<20 for values in samples.values()):raise ValueError('insufficient standing reference samples')
        profile=validate_proportions({name:float(np.median(values))for name,values in samples.items()})
        profiles.append(profile);counts.append({name:len(values)for name,values in samples.items()})
    if not profiles:raise ValueError('at least one explicit standing window required')
    combined=validate_proportions({name:float(np.median([profile[name]for profile in profiles]))for name in groups})
    return combined,counts


@dataclass(frozen=True, slots=True)
class _ArmMotion:
    directions: tuple[tuple[float, float, float], ...]
    inferred: bool
    catching_up: bool
    display_timestamp_ns: int


@dataclass(frozen=True, slots=True)
class RigidAvatarPreview:
    render_plan: object
    inferred_slots: tuple[int,...]
    reason: str
    body_scale: float | None
    head_mode: str
    endpoint_errors: tuple[tuple[str,float],...]=()
    display_transform: tuple[float,...]=IDENTITY_4
    display_body_scale: float | None=None


class PreviewRigidAvatar:
    """A fixed-proportion render skeleton, including explicitly inferred parts.

    NumPy is the existing optional preview dependency. Missing observations are
    never filled in the source plan. Reference/held results must be labelled by
    the consumer; they do not represent a newly observed complete person.
    """

    def __init__(self, asset, bone_links, topology_indices, *, proportions=None,
                 affine_composer=None,bone_affine_builder=None,limb_axis_builder=None,head_cue_hold=False,
                 head_current_axes=False,finite_arm_reach=False,leg_recovery=False,math_builder=None,tail_library=None,
                 native_limb_builder=None):
        if type(head_cue_hold)is not bool:raise ValueError('head_cue_hold must be boolean')
        if type(head_current_axes)is not bool:raise ValueError('head_current_axes must be boolean')
        if type(finite_arm_reach)is not bool:raise ValueError('finite_arm_reach must be boolean')
        if type(leg_recovery)is not bool:raise ValueError('leg_recovery must be boolean')
        self._matmul=_matmul
        self.math_builder=math_builder
        if math_builder is not None:
            if not callable(getattr(math_builder,'matmul',None)) or not callable(getattr(math_builder,'canonical',None)):
                raise TypeError('rig math builder requires callable matmul and canonical')
            self._matmul=math_builder.matmul
            self._canonical=math_builder.canonical
        self.head_cue_hold=head_cue_hold
        self.head_current_axes=head_current_axes
        self.finite_arm_reach=finite_arm_reach
        self.leg_recovery=leg_recovery
        import numpy as np
        self.np=np;self.names=tuple(asset.bone_names);self.links=tuple(bone_links)
        self.indices=tuple(topology_indices);self.slot={n:i for i,n in enumerate(self.names)}
        required={'spine','neck','head'}|{side+'_'+part for side in ('left','right')
            for part in ('upper_arm','lower_arm','upper_leg','lower_leg','hand')}
        if (len(self.names)!=13 or set(self.names)!=required or len(self.indices)!=13 or
                any(type(i)is not int or not 0<=i<len(self.links)for i in self.indices) or
                any(self.links[i].name!=name for i,name in zip(self.indices,self.names))):
            raise ValueError('complete rig requires explicit thirteen-bone topology')
        self.proportions=validate_proportions(dict.fromkeys(PROPORTION_KEYS,1.) if proportions is None else proportions)
        self.length=np.array(asset.bone_bind_lengths_q1_14,dtype=float)/16384
        self.inverse=tuple(asset.inverse_bind_matrices_q1_14);self.bind=[]
        for matrix in self.inverse:
            self.bind.append(_inverse_bind(matrix))
        self.start={name:self.bind[i][:3,3]for i,name in enumerate(self.names)}
        self.end={name:(self._matmul(self.bind[i],np.array([0.,self.length[i],0.,1.])))[:3]
                  for i,name in enumerate(self.names)}
        self.origin=self.start['spine']
        self.base=self._basis(self.start['left_upper_arm']-self.start['right_upper_arm'],
                              self.start['head']-self.origin)
        self.torso_length=float(_norm(self.start['head']-self.origin))
        self.compose=compose_affine_q1_14 if affine_composer is None else affine_composer
        if not callable(self.compose):raise TypeError('affine composer must be callable')
        self.bone_affine=bone_affine_matrix_q1_14 if bone_affine_builder is None else bone_affine_builder
        if not callable(self.bone_affine):raise TypeError('bone affine builder must be callable')
        if limb_axis_builder is not None and not callable(limb_axis_builder):
            raise TypeError('limb axis builder must be callable')
        self.limb_axis_builder=limb_axis_builder
        self.heading=PreviewTorsoHeading(self.links,self.indices,asset.bone_bind_lengths_q1_14,
                                        self.inverse,affine_composer=affine_composer,bone_affine_builder=bone_affine_builder)
        self.reset()
        self._native_tail=None
        if tail_library is not None:
            from .native_rig_tail import NativeRigTail
            self._native_tail=NativeRigTail(self,tail_library)
        # Optional factory binds only immutable geometry once; default math
        # and every existing policy/history/tail path remain original.
        self.native_limb_builder=None
        if native_limb_builder is not None:
            self.native_limb_builder=native_limb_builder(self)
            if not callable(self.native_limb_builder):
                raise TypeError('native limb factory must return a callable batch')

    def reset(self):
        self._body_motion=None;self._timestamp=None;self._display_timestamp=None;self._last_good=None;self._last_good_ns=None
        self._head_twist=None;self._head_direction=None;self._head_cue_ready_ns=None;self._head_cue_target=None
        self._arm_motion_history={};self._leg_motion_history={};self._bone_history={};self._hand_history={};self.heading.reset()

    def _unit(self,vector):
        norm=float(_norm(vector))
        if not math.isfinite(norm) or norm<1e-7:raise ValueError('degenerate rig direction')
        return vector/norm

    def _basis(self,lateral,vertical):
        y=self._unit(vertical);x=self._unit(lateral-_dot(lateral,y)*y)
        return self.np.column_stack((x,y,_cross(x,y)))

    def _point(self,joints,name):
        point=joints.get(name)
        if point is None or not point.valid:raise ValueError('missing rig input joint')
        result=self.np.array(point.position,dtype=float)
        if not self.np.isfinite(result).all():raise ValueError('nonfinite rig input joint')
        return result

    def _ik(self,start,hint,target,l1,l2):
        np=self.np;offset=target-start;reach=l1+l2;xy=float(_norm(offset[:2]))
        offset=offset.copy()
        if xy>reach:offset[:2]*=reach/xy;offset[2]=0.
        else:
            depth=math.sqrt(max(0.,reach*reach-xy*xy));offset[2]=max(-depth,min(depth,float(offset[2])))
        distance=float(_norm(offset))
        if distance<1e-7:
            offset=np.array([0.,-(abs(l1-l2)+1e-5),0.]);distance=float(_norm(offset))
        direction=offset/distance;radius=float(max(abs(l1-l2)+1e-7,min(reach,distance)))
        plane=hint-start-_dot(hint-start,direction)*direction
        if _norm(plane)<1e-7:
            seed=np.eye(3)[int(np.argmin(np.abs(direction)))];plane=seed-_dot(seed,direction)*direction
        plane=self._unit(plane);along=(radius*radius+l1*l1-l2*l2)/(2*radius)
        middle=start+along*direction+math.sqrt(max(0.,l1*l1-along*along))*plane
        return middle,start+radius*direction

    @staticmethod
    def _canonical(vector):
        # Display geometry has a much finer grid than the final Q1.14 palette.
        # Canonical inputs avoid architecture-dependent antipodal palm branches.
        return tuple(round(float(value)*16777216)/16777216 for value in vector)

    def _face_axes(self,observed,display_ns):
        np=self.np;pair=observed.pair
        age=display_ns-max(pair.front.timestamp_ns,pair.side.timestamp_ns)
        axes=[]
        if not 0<=age<=100_000_000:return axes
        for part in ('eye','ear'):
            names=('left_'+part,'right_'+part)
            if not all(self.heading._visible(frame.keypoints.get(n))
                       for frame in (pair.front,pair.side)for n in names):continue
            try:
                delta=self._point(observed.joints,names[0])-self._point(observed.joints,names[1])
                if _norm(delta)>.025:axes.append(self._unit(delta))
            except ValueError:continue
        if len(axes)>1 and _dot(axes[0],axes[1])<math.cos(math.radians(30)):return []
        return axes

    def _current_face_axes(self,observed,source,display_ns):
        pair=observed.pair;age=display_ns-max(pair.front.timestamp_ns,pair.side.timestamp_ns)
        if not 0<=age<=100_000_000:return []
        axes=[];front,side=pair.front,pair.side;sign=source.side_forward_sign
        for part in('eye','ear'):
            names=('left_'+part,'right_'+part)
            if not all(self.heading._visible(f.keypoints.get(n))for f in(front,side)for n in names):continue
            a,b=(front.keypoints[n]for n in names);c,d=(side.keypoints[n]for n in names)
            fw,fh=front.source_width,front.source_height;sw,sh=side.source_width,side.source_height
            delta=self.np.array(((a.x-b.x)*2*fw/max(fw,fh),-(a.y-b.y)*2*fh/max(fw,fh),
                -(c.x-d.x)*sign*2*sw/max(sw,sh)))
            if _norm(delta)>.025:axes.append(self._unit(delta))
        if len(axes)>1 and _dot(axes[0],axes[1])<math.cos(math.radians(30)):return []
        return axes

    def _head_up(self,observed,rotation,display_ns,dt):
        np=self.np;up=self._unit(self._matmul(rotation,self.bind[self.slot['head']][:3,1]))
        axes=self._face_axes(observed,display_ns)
        if axes:
            lateral=self._unit(sum(axes));vertical=np.array([0.,1.,0.])
            projected=vertical-_dot(vertical,lateral)*lateral
            if _norm(projected)>.7:up=self._unit(projected)
            # An ear-to-eye side slope supports nodding independently of nose XY.
            angles=[];side=observed.pair.side
            for name in ('left','right'):
                ear=side.keypoints.get(name+'_ear');eye=side.keypoints.get(name+'_eye')
                if not self.heading._visible(ear)or not self.heading._visible(eye):continue
                forward=abs(eye.x-ear.x)*side.source_width
                if forward/max(side.source_width,side.source_height)<.015:continue
                angle=math.atan2((ear.y-eye.y)*side.source_height,forward)
                if abs(angle)<=math.radians(45):angles.append(angle)
            if angles and (len(angles)==1 or abs(angles[0]-angles[1])<math.radians(20)):
                pitch=sum(angles)/len(angles);back=self._unit(_cross(lateral,up))
                up=self._unit(math.cos(pitch)*up+math.sin(pitch)*back)
        if self._head_direction is not None:
            old=self.np.array(self._head_direction);angle=math.acos(float(max(-1.,min(1.,_dot(old,up)))))
            if angle<math.radians(1):up=old
            else:
                step=min(angle*(1.-math.exp(-2.*math.pi*3.*dt)),2.*dt)
                tangent=up-_dot(up,old)*old
                if _norm(tangent)>1e-7:up=math.cos(step)*old+math.sin(step)*self._unit(tangent)
                else:up=old
        return self._unit(up)

    def _head_reference(self,observed,rotation,direction,display_ns,dt,*,face_source=None):
        np=self.np;base=self._unit(self._matmul(rotation,self.bind[self.slot['head']][:3,0]))
        y=self._unit(direction)
        projected=base-_dot(base,y)*y
        if _norm(projected)<1e-7:
            seed=rotation[:,int(np.argmin(np.abs(self._matmul(rotation.T,y))))]
            projected=seed-_dot(seed,y)*y
        base=self._unit(projected)
        if self.head_current_axes:
            from .preview_face_source import FaceAxisSource
            if (type(face_source)is not FaceAxisSource or not face_source.matches(observed.pair)or
                    face_source.side_forward_sign is None):
                self._head_cue_ready_ns=None;self._head_cue_target=None;axes=[]
            else:axes=self._current_face_axes(observed,face_source,display_ns)
        else:axes=self._face_axes(observed,display_ns)
        angle=0.;mode='torso_heading'
        if axes and (len(axes)==1 or _dot(axes[0],axes[1])>math.cos(math.radians(30))):
            target=self._unit(sum(axes));projected=target-_dot(target,y)*y
            if _norm(projected)>1e-7:
                target=self._unit(projected)
                value=math.atan2(float(_dot(y,_cross(base,target))),float(_dot(base,target)))
                if abs(value)<math.radians(80):angle=value;mode='face_lateral'
        if self.head_cue_hold:
            ready=max(observed.pair.front.timestamp_ns,observed.pair.side.timestamp_ns)
            if mode=='face_lateral':
                if self._head_cue_ready_ns is None or ready>self._head_cue_ready_ns:
                    self._head_cue_ready_ns=ready;self._head_cue_target=angle
            elif (self._head_cue_ready_ns is not None and ready>=self._head_cue_ready_ns and
                  0<=display_ns-self._head_cue_ready_ns<=100_000_000):
                angle=self._head_cue_target;mode='held_face_lateral'
        if self._head_twist is not None:
            change=(angle-self._head_twist+math.pi)%(2*math.pi)-math.pi
            step=change*(1.-math.exp(-2.*math.pi*3.*dt))
            angle=self._head_twist+max(-2.*dt,min(2.*dt,step))
        reference=math.cos(angle)*base+math.sin(angle)*_cross(y,base)
        return self._unit(reference),mode,angle

    def _limb_axis(self,slot,segment,rotation,dt):
        if self.limb_axis_builder is not None:
            result=self.limb_axis_builder(slot,segment,rotation,dt,self.bind,self._bone_history)
            if result is not NotImplemented:return result
        np=self.np;direction=self._unit(self._matmul(rotation.T,(np.array(segment.end)-np.array(segment.start))))
        reference=self.bind[slot][:3,0]
        desired=reference-_dot(reference,direction)*direction;strength=float(_norm(desired))
        previous=self._bone_history.get(slot)
        if previous is None:
            axis=np.array(stable_bone_local_x_axis(segment,tuple(self._matmul(rotation,reference))))
            local=self._unit(self._matmul(rotation.T,axis))
        else:
            local,old_direction=previous;cross=_cross(old_direction,direction)
            cosine=float(max(-1.,min(1.,_dot(old_direction,direction))))
            if cosine>-1.+1e-8:
                local=local+_cross(cross,local)+_cross(cross,_cross(cross,local))/(1.+cosine)
            local=self._unit(local-_dot(local,direction)*direction)
            if strength>1e-7:
                desired/=strength
                angle=math.atan2(float(_dot(direction,_cross(local,desired))),float(_dot(local,desired)))
                limit=8.*dt*min(1.,strength/.5);angle=max(-limit,min(limit,angle))
                local=self._unit(math.cos(angle)*local+math.sin(angle)*_cross(direction,local))
        return tuple(self._matmul(rotation,local)),(local,direction)

    def _arm_motion(self,slot,start,middle,end,rotation,l1,l2,*,inferred,supported_depth,display_ns,motion_history=None):
        """Ease model reference transitions; source/scoring observations stay read-only."""
        np=self.np
        desired=(self._unit(self._matmul(rotation.T,middle-start)),self._unit(self._matmul(rotation.T,end-middle)))
        previous=(self._arm_motion_history if motion_history is None else motion_history).get(slot)
        active=False;filtered=False;directions=desired;target_depth=float((end-start)[2])
        if previous is not None:
            old=previous.directions
            elapsed=(display_ns-previous.display_timestamp_ns)/1e9
            needs=inferred or previous.inferred or previous.catching_up
            old_depth=float(self._matmul(rotation,l1*np.array(old[0])+l2*np.array(old[1]))[2])
            if supported_depth and abs(old_depth)>.025 and abs(target_depth)>.025 and old_depth*target_depth<0:
                needs=False
            if needs and 0<=elapsed<=.25:
                result=[];filtered=True
                for a,b in zip(old,desired):
                    a=self._unit(np.array(a));angle=math.acos(max(-1.,min(1.,_dot(a,b))))
                    if angle<=math.radians(1):result.append(b);continue
                    step=min(angle*(1.-math.exp(-2.*math.pi*6.*elapsed)),8.*elapsed)
                    tangent=b-_dot(a,b)*a
                    if _norm(tangent)<1e-7:
                        seed=np.eye(3)[int(np.argmin(np.abs(a)))];tangent=seed-_dot(seed,a)*a
                    result.append(self._unit(math.cos(step)*a+math.sin(step)*self._unit(tangent)))
                    active|=step+1e-7<angle
                directions=tuple(result)
        if filtered:
            next_middle=start+l1*self._matmul(rotation,directions[0])
            next_end=next_middle+l2*self._matmul(rotation,directions[1])
        else:
            next_middle,next_end=middle,end
        if supported_depth and abs(target_depth)>.025 and target_depth*float((next_end-start)[2])<=0:
            next_middle,next_end=middle,end;directions=desired;active=False
        history=_ArmMotion(tuple(self._canonical(v)for v in directions),inferred,active,display_ns)
        return next_middle,next_end,history,active

    def _leg_motion(self,slot,start,middle,end,l1,l2,*,display_transform,inferred,body_recovering,display_ns):
        identity = type(display_transform) is tuple and display_transform == IDENTITY_4
        linear = None if identity else tuple(float(display_transform[k])for k in (0,1,2,4,5,6,8,9,10))
        l1,l2 = float(l1),float(l2)
        start3 = tuple(map(float,start))
        first = tuple(float(middle[k])-start3[k]for k in range(3))
        second = tuple(float(end[k])-float(middle[k])for k in range(3))
        outer = (_leg_unit(first),_leg_unit(second)) if identity else (_leg_unit(_leg_matvec(linear,first)),_leg_unit(_leg_matvec(linear,second)))
        # Preserve the original helper's observable FP operation order: its
        # second direction was recovered from (u0+u1)-u0, then renormalized.
        summed = tuple(a+b for a,b in zip(*outer))
        desired = (_leg_unit(outer[0]),_leg_unit(tuple(summed[k]-outer[0][k]for k in range(3))))
        previous = self._leg_motion_history.get(slot)
        inferred = inferred or body_recovering
        elapsed = (display_ns-previous.display_timestamp_ns)/1e9 if previous else None
        follows = previous is not None and (inferred or previous.inferred or previous.catching_up) and 0<=elapsed<=.25
        directions = desired
        active = False
        if follows:
            followed = []
            response = 1.-math.exp(-2.*math.pi*6.*elapsed)
            for a,b in zip(previous.directions,desired):
                a = _leg_unit(a)
                cosine = _leg_dot(a,b)
                angle = math.acos(max(-1.,min(1.,cosine)))
                if angle <= math.radians(1):
                    followed.append(b)
                    continue
                step = min(angle*response,8.*elapsed)
                tangent = tuple(b[k]-cosine*a[k]for k in range(3))
                if math.sqrt(_leg_dot(tangent,tangent)) < 1e-7:
                    axis = min(range(3),key=lambda k:abs(a[k]))
                    tangent = tuple(float(k==axis)-a[axis]*a[k]for k in range(3))
                tangent = _leg_unit(tangent)
                c,s = math.cos(step),math.sin(step)
                followed.append(_leg_unit(tuple(c*a[k]+s*tangent[k]for k in range(3))))
                active |= step+1e-7 < angle
            directions = tuple(followed)
            # The old helper added the two links then subtracted its first
            # endpoint before inverse mapping. Keep that rounding, without arrays.
            combined = tuple(a+b for a,b in zip(*directions))
            second_followed = tuple(combined[k]-directions[0][k]for k in range(3))
            if identity:
                u,v = _leg_unit(directions[0]),_leg_unit(second_followed)
            else:
                inverse = _leg_inverse3(linear)
                u,v = _leg_unit(_leg_matvec(inverse,directions[0])),_leg_unit(_leg_matvec(inverse,second_followed))
            mid = tuple(start3[k]+l1*u[k]for k in range(3))
            tip = tuple(mid[k]+l2*v[k]for k in range(3))
            # Two owned result arrays are required by the unchanged rig API;
            # no NumPy temporaries, identity matrices or linear solver are used.
            middle,end = self.np.array(mid),self.np.array(tip)
        history = _ArmMotion(tuple(self._canonical(v)for v in directions),inferred,active,display_ns)
        return middle,end,history,active

    def _neutral_leg_history(self,plan,display_transform,display_ns):
        np=self.np;linear=np.asarray(display_transform).reshape(4,4)[:3,:3];history={}
        for side in ('left','right'):
            h,k,a=(self._point(plan.joints,side+'_'+name)for name in ('hip','knee','ankle'))
            directions=tuple(self._canonical(self._unit(self._matmul(linear,v)))for v in (k-h,a-k))
            history[self.slot[side+'_upper_leg']]=_ArmMotion(directions,True,False,display_ns)
        return history

    def _source_clock(self,observed,render):
        if observed.pair is not render.pair:raise ValueError('complete rig requires the same source pair')
        pair=observed.pair
        return ((pair.front.timestamp_ns+pair.side.timestamp_ns)//2,
                max(pair.front.timestamp_ns,pair.side.timestamp_ns))

    def _supported_depth(self,observed,side,supported):
        return supported and all(self.heading._visible(frame.keypoints.get(side+'_wrist'))
                                 for frame in (observed.pair.front,observed.pair.side))

    def _body_rotation(self,observed,yaw,pelvis,neck):
        return self._matmul(self._basis(self.np.array([math.cos(yaw),0.,math.sin(yaw)]),
                                   neck-pelvis),self.base.T)

    def _held_render_plan(self,previous,render):
        return replace(previous,pair=render.pair,points=render.points)

    def apply(self,observed,render,*,display_timestamp_ns,face_source=None):
        timestamp,ready=self._source_clock(observed,render)
        recovered_legs=()
        if self.leg_recovery:
            from .preview_leg_recovery import recover_current_legs
            render,recovered_legs=recover_current_legs(observed,render,self.slot,face_source=face_source,
                display_ns=display_timestamp_ns)
        previous=self._timestamp
        if previous is not None and timestamp<previous:raise ValueError('non-monotonic complete rig timestamp')
        if self._display_timestamp is not None and display_timestamp_ns<self._display_timestamp:
            raise ValueError('non-monotonic complete rig display timestamp')
        if previous is not None and timestamp-previous>250_000_000:self.reset();previous=None
        self._timestamp=timestamp;self._display_timestamp=display_timestamp_ns
        dt=(timestamp-previous)/1e9 if previous is not None else 0.
        np=self.np;all_slots=tuple(range(13));inferred=[];errors=[];neutral=False;arm_motion_updates={};leg_motion_updates={}
        heading=self.heading.apply(observed,render,display_timestamp_ns=display_timestamp_ns)
        try:
            if not 0<=display_timestamp_ns-ready<=100_000_000:
                raise ValueError('current torso source is stale')
            if not all(observed.matrix_valid[self.slot[n]] and render.matrix_valid[self.slot[n]]for n in ('spine','neck')):
                raise ValueError('current torso unavailable')
            pelvis=self._point(observed.joints,'pelvis');neck=self._point(observed.joints,'neck')
            scale=float(_norm(neck-pelvis))/self.torso_length
            if not .5<=scale<=1.5:raise ValueError('unreliable torso size')
            yaw=heading.yaw_radians or 0.
            rotation=self._body_rotation(observed,yaw,pelvis,neck)
        except ValueError:
            if self._last_good is not None and 0<=display_timestamp_ns-self._last_good_ns<=100_000_000:
                held=self._held_render_plan(self._last_good.render_plan,render)
                result=RigidAvatarPreview(held,all_slots,'held_body',self._last_good.body_scale,'reference',
                    display_transform=self._last_good.display_transform,display_body_scale=self._last_good.display_body_scale)
                if self._body_motion is not None:self._body_motion=replace(self._body_motion,inferred=True,display_timestamp_ns=display_timestamp_ns)
                return result
            pelvis=np.zeros(3);rotation=np.eye(3);scale=1.;neutral=True;inferred.extend(self.names)
        source_frame=BodyFrame(tuple(pelvis),tuple(rotation.flat),scale)
        frame,body_motion=body_reference_frame(None if neutral else source_frame,
            self._body_motion,display_timestamp_ns=display_timestamp_ns,bind_lateral=tuple(self.base[:,0]))
        display_transform=display_affine(source_frame,frame)
        transform=lambda value:pelvis+scale*self._matmul(rotation,(value-self.origin))
        # Limbs, hands and head are assigned below; do not transform unused bind endpoints.
        limb_builder=self.native_limb_builder
        if limb_builder is None:
            poses={name:(transform(self.start[name]),transform(self.end[name]))for name in ('spine','neck')}
        else:
            torso_batch,limb_batch,batch_inferred=limb_builder(observed,render,pelvis,rotation,scale,neutral)
            poses={'spine':(torso_batch[0],torso_batch[1]),'neck':(torso_batch[2],torso_batch[3])}
        for side in ('left','right'):
            for limb,parent,child,tip in (('arm','shoulder','elbow','wrist'),('leg','hip','knee','ankle')):
                upper=f'{side}_upper_{limb}';lower=f'{side}_lower_{limb}'
                if limb_builder is None:
                    i,j=self.slot[upper],self.slot[lower];start=transform(self.start[upper])
                    l1=scale*self.length[i]*self.proportions['upper_'+limb]
                    l2=scale*self.length[j]*self.proportions['lower_'+limb]
                    leg_directions=None
                    try:
                        if neutral:raise ValueError('untracked chain')
                        driver=render.joints
                        if not all(render.matrix_valid[k]for k in (i,j)):
                            if limb!='arm':raise ValueError('untracked leg chain')
                            names=tuple(side+'_'+part for part in (parent,child,tip))
                            if (not all(observed.segments[self.indices[k]].valid for k in (i,j))or
                                    not all(n in observed.joints and observed.joints[n].valid and
                                        observed.joints[n].confidence>=.5 for n in names)):
                                raise ValueError('missing current chain')
                            driver=observed.joints;inferred.extend((upper,lower))
                        root=self._point(driver,side+'_'+parent)
                        hint=start+self._point(driver,side+'_'+child)-root
                        target=start+self._point(driver,side+'_'+tip)-root
                        if not all(observed.matrix_valid[k]for k in (i,j)):inferred.extend((upper,lower))
                        if limb=='leg':leg_directions=(self._unit(hint-start),self._unit(target-hint))
                    except ValueError:
                        inferred.extend((upper,lower))
                        reach=.99975 if limb=='leg' else .96
                        target=start+(l1+l2)*reach*self._matmul(rotation,self._unit(np.array([0.,-1.,-.1])))
                        hint=start+self._matmul(rotation,np.array([.05 if side=='left'else-.05,-l1,-.05]))
                    if leg_directions is not None:
                        middle=start+l1*leg_directions[0];end=middle+l2*leg_directions[1]
                    else:
                        ik_target=target
                        if limb=='arm'and self.finite_arm_reach:
                            from .preview_arm_reach import project_arm_reach
                            ik_target=start+np.array(project_arm_reach(target-start,l1+l2))
                        middle,end=self._ik(start,hint,ik_target,l1,l2)
                else:
                    i,j=self.slot[upper],self.slot[lower]
                    chain_index=(0 if side=='left'else 2)+(limb=='leg')
                    start,hint,target,middle,end,l1,l2,_=limb_batch[chain_index]
                    inferred.extend(batch_inferred[chain_index])
                if limb=='leg'and not neutral and self.leg_recovery:
                    if i in recovered_legs:inferred.extend((upper,lower))
                    middle,end,history,active=self._leg_motion(i,start,middle,end,l1,l2,
                        display_transform=display_transform,inferred=upper in inferred or lower in inferred,
                        body_recovering=body_motion.inferred or body_motion.catching_up,display_ns=display_timestamp_ns)
                    leg_motion_updates[i]=history
                    if active or body_motion.inferred or body_motion.catching_up:inferred.extend((upper,lower))
                if limb=='arm'and not neutral:
                    supported=all(observed.matrix_valid[k]for k in (i,j))
                    supported_depth=self._supported_depth(observed,side,supported)
                    middle,end,history,_=self._arm_motion(i,start,middle,end,rotation,l1,l2,
                        inferred=upper in inferred or lower in inferred,supported_depth=supported_depth,display_ns=display_timestamp_ns)
                    arm_motion_updates[i]=history
                poses[upper]=(start,middle);poses[lower]=(middle,end)
                errors.append((side+'_'+limb,float(_norm(end-target))))
                if limb=='arm':
                    hand=side+'_hand';poses[hand]=(end,end+scale*self.length[self.slot[hand]]*self._unit(end-middle))
                    if (upper in inferred or lower in inferred or not observed.matrix_valid[self.slot[hand]]
                            or not render.matrix_valid[self.slot[hand]]):inferred.append(hand)
        head_slot=self.slot['head'];head_start=transform(self.start['head'])
        if not neutral:
            direction=self._head_up(observed,rotation,display_timestamp_ns,dt)
            if not observed.matrix_valid[head_slot]or not render.matrix_valid[head_slot]:inferred.append('head')
        else:direction=self._matmul(rotation,self.bind[head_slot][:3,1]);inferred.append('head')
        poses['head']=(head_start,head_start+scale*self.length[head_slot]*self._unit(direction))
        try:
            if neutral:head_reference=self._matmul(rotation,self.bind[head_slot][:3,0]);head_mode='reference';twist=0.
            elif self.head_current_axes:
                head_reference,head_mode,twist=self._head_reference(observed,rotation,direction,display_timestamp_ns,dt,
                    face_source=face_source)
            else:head_reference,head_mode,twist=self._head_reference(observed,rotation,direction,display_timestamp_ns,dt)
            if neutral:
                self._bone_history.clear();self._hand_history.clear();self._arm_motion_history.clear();self._head_direction=None
                self._head_cue_ready_ns=None;self._head_cue_target=None
            if self._native_tail is not None:
                plan,bone_updates,hand_updates=self._native_tail.finish(
                    render,observed,poses,scale,rotation,dt,head_reference,inferred)
            else:
                writes=list(render.matrix_writes);joints={};bone_updates={};hand_updates={}
                for slot,name in enumerate(self.names):
                    start,end=poses[name];link=self.links[self.indices[slot]];length=float(_norm(end-start))
                    segment=BoneSegment3D(name,link.parent,link.child,self._canonical(start/scale),self._canonical(end/scale),length/scale,0. if name in inferred else 1.,True)
                    if name.endswith('_hand'):
                        previous_hand=self._hand_history.get(slot)
                        axis=continuous_palm_down_x_axis(segment,
                            previous_axis=previous_hand[0]if previous_hand else None,
                            previous_direction=previous_hand[1]if previous_hand else None,max_roll_radians=12.*dt)
                        hand_updates[slot]=(self._canonical(axis),self._canonical(self._unit(np.array(segment.end)-np.array(segment.start))))
                    elif 'arm'in name or 'leg'in name:
                        axis,history=self._limb_axis(slot,segment,rotation,dt);bone_updates[slot]=tuple(np.array(self._canonical(v))for v in history)
                    else:
                        reference=tuple(head_reference)if name=='head'else tuple(self._matmul(rotation,self.bind[slot][:3,0]))
                        axis=stable_bone_local_x_axis(segment,reference)
                    matrix=self.bone_affine(segment,world_to_model_scale=scale,local_x_reference=self._canonical(axis),
                        local_bone_length_q1_14=None if name=='head'else int(round(self.length[slot]*16384)))
                    writes[slot]=MatrixWrite(slot,self.compose(matrix,self.inverse[slot]))
                    confidence=0. if name in inferred else min((observed.joints[n].confidence for n in (link.parent,link.child)if n in observed.joints),default=0.)
                    for joint_name,position in ((link.parent,start),(link.child,end)):
                        joints[joint_name]=SkeletonJoint3D(joint_name,tuple(float(v)for v in position),confidence,math.inf,True)
                valid=tuple(i<13 for i in range(16))
                plan=replace(render,joints=joints,segments=build_bone_segments(joints,self.links),
                             matrix_writes=tuple(writes),matrix_valid=valid)
            if self.head_cue_hold and head_mode=='held_face_lateral':
                # The neck is shared with the current torso. Only the derived
                # head child and head segment lose confidence during a cue hold.
                inferred.append('head');index=self.indices[head_slot]
                segments=list(plan.segments);segments[index]=replace(segments[index],confidence=0.)
                child=self.links[index].child;joints=dict(plan.joints)
                joints[child]=replace(joints[child],confidence=0.)
                plan=replace(plan,joints=joints,segments=tuple(segments))
        except ValueError:
            self._body_motion=None;self._last_good=None;self._last_good_ns=None;self._head_twist=None;self._head_direction=None
            self._head_cue_ready_ns=None;self._head_cue_target=None
            self._bone_history.clear();self._hand_history.clear();self._arm_motion_history.clear();self._leg_motion_history.clear();self.heading.reset()
            return RigidAvatarPreview(render,(),'rejected',None,'unavailable')
        self._body_motion=body_motion
        self._head_twist=twist;self._head_direction=self._canonical(self._unit(direction))if not neutral else None
        self._bone_history=bone_updates;self._hand_history=hand_updates;self._arm_motion_history=arm_motion_updates
        if self.leg_recovery:
            self._leg_motion_history=(self._neutral_leg_history(plan,display_transform,display_timestamp_ns)
                if neutral else leg_motion_updates)
        result=RigidAvatarPreview(plan,tuple(sorted({self.slot[n]for n in inferred})),
                                  'neutral'if neutral else 'partial'if inferred else 'tracked',scale,head_mode,tuple(errors),
                                  display_transform,frame.scale)
        if not neutral:self._last_good=result;self._last_good_ns=ready
        return result
