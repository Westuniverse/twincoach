# SPDX-License-Identifier: GPL-2.0-only
"""Cold camera-bound display geometry before the existing world stabilizer."""
import math
from collections.abc import Mapping
from types import MappingProxyType
import numpy as np
from posepriv_coach.avatar_calibration import AvatarCalibration
from posepriv_coach.triangulation import _distort_normalized
from posepriv_coach.world_avatar_preview import WorldDisplayObservation

SCHEMA = 'posepriv.two_view_display_camera_profile.v1'
VIEWS = ('front', 'side')
TORSO = ('left_shoulder','right_shoulder','left_hip','right_hip')
SEGMENTS = {s:((s+'_shoulder',s+'_elbow',11+o,13+o,'upper_arm'),
               (s+'_elbow',s+'_wrist',13+o,15+o,'lower_arm')) for s,o in [('left',0),('right',1)]}


def unit(v):
    length=float(np.linalg.norm(v))
    if length<1e-10:raise ValueError('degenerate direction')
    return v/length


class TwoViewGeometryInput:
    """Selected math leaf; primary view and expected source geometry are cold."""
    def __init__(self,calibration,rig,*,primary_view,source_geometry):
        self.cal=AvatarCalibration.from_mapping(calibration)
        self.primary_view=primary_view
        self.expected_geometry={v:(v,source_geometry[v]['width'],source_geometry[v]['height'],
                                   source_geometry[v]['rotation_degrees']) for v in VIEWS}
        self.K=dict(front=self.cal.front_intrinsics,side=self.cal.side_intrinsics)
        for v,k in self.K.items():
            sx=source_geometry[v]['width']/k.width;sy=source_geometry[v]['height']/k.height
            if abs(sx-sy)/max(sx,sy)>.005:raise ValueError('different_calibration_field_of_view')
        self.R={v:np.array(e.rotation_camera_to_world).reshape(3,3) for v,e in
                [('front',self.cal.front_extrinsics),('side',self.cal.side_extrinsics)]}
        self.flip=np.diag([1.,-1.,1.])
        self.relative={v:self.R[v].T@self.R[primary_view]@self.flip for v in VIEWS}
        shoulder=(rig.start['left_upper_arm']+rig.start['right_upper_arm'])/2
        hip=(rig.start['left_upper_leg']+rig.start['right_upper_leg'])/2
        self.torso_length=float(np.linalg.norm(shoulder-hip))
        self.length={(side,part):float(rig.length[rig.slot[side+'_'+part]])
                     for side in SEGMENTS for part in ('upper_arm','lower_arm')}

    def ray(self,view,point):
        k=self.K[view];xd=(point.x*k.width-k.cx)/k.fx;yd=(point.y*k.height-k.cy)/k.fy
        try:x,y=k.undistort_normalized(xd,yd)
        except ValueError:
            x,y=xd,yd;k1,k2,p1,p2,k3=k.distortion
            for _ in range(64):
                r=x*x+y*y;radial=1+k1*r+k2*r*r+k3*r*r*r
                x,y=(xd-2*p1*x*y-p2*(r+2*x*x))/radial,(yd-p1*(r+2*y*y)-2*p2*x*y)/radial
            xx,yy=_distort_normalized(x,y,k.distortion)
            if math.hypot(xx-xd,yy-yd)>1e-7:raise ValueError('undistortion residual')
        return np.array([x,y,1.])

    def apply(self,primary,work):
        meta=dict(active=False,scope='display_only_current_two_view_before_existing_filter',arms={})
        if not primary.world_landmarks:return primary,meta
        observations=work['world_observations'];poses=work['poses'];cache={v:{} for v in VIEWS}
        def ray(v,name):
            if name not in cache[v]:
                p=poses[v].keypoints.get(name)
                if p is None or p.confidence<.5:raise ValueError('q_below_0p5')
                if not(0<=p.x<=1 and 0<=p.y<=1):raise ValueError('outside_source')
                cache[v][name]=self.ray(v,p)
            return cache[v][name]
        for v,expected in self.expected_geometry.items():
            source=observations[v].source
            if (source.view,source.width,source.height,source.rotation_degrees)!=expected:
                meta['reason']='different_camera_geometry';return primary,meta
        try:
            torso={v:((ray(v,'left_shoulder')+ray(v,'right_shoulder'))/2,
                      (ray(v,'left_hip')+ray(v,'right_hip'))/2) for v in VIEWS}
            normals={v:unit(self.R[v]@np.cross(hip,shoulder)) for v,(shoulder,hip) in torso.items()}
            up=unit(np.cross(normals['front'],normals['side']))
            signs=[]
            for v,(shoulder,hip) in torso.items():
                camera=self.R[v].T@up;projected=camera[:2]-hip[:2]*camera[2]
                signs.append(float(projected@(shoulder-hip)[:2]))
            if signs[0]*signs[1]<=0:raise ValueError('torso_direction_conflict')
            if signs[0]<0:up=-up
            torso_model=(self.R[self.primary_view].T@up)*np.array([1.,-1.,1.])*self.torso_length
            scales={}
            for v,(shoulder,hip) in torso.items():
                J=np.array([[1.,0.,-hip[0]],[0.,1.,-hip[1]]])@self.relative[v]
                projected=J@torso_model;actual=(shoulder-hip)[:2]
                scales[v]=float(projected@actual/(projected@projected))
                if scales[v]<=0:raise ValueError('torso_scale_conflict')
        except (ValueError,np.linalg.LinAlgError) as exc:
            meta['reason']=str(exc);return primary,meta
        points=primary.world_landmarks;changed={}
        point=lambda index:np.array([points[index][k] for k in ('x','y','z')])
        for side,segments in SEGMENTS.items():
            directions=[];weights=[]
            for parent,child,i,j,part in segments:
                original_world=point(j)-point(i)
                try:original=unit(original_world*self.flip.diagonal())
                except ValueError:
                    # Preserve this entire input arm for the existing rig's
                    # recovery path; the other arm remains independent.
                    for *_,arm_part in segments:
                        meta['arms'][side+'_'+arm_part]=dict(weight=0.,reason='degenerate_original_arm')
                    directions=[]
                    break
                info=dict(weight=0.,reason='unchanged')
                try:
                    matrices=[];targets=[]
                    for v in VIEWS:
                        p0,p1=ray(v,parent),ray(v,child);k=self.K[v]
                        q=math.sqrt(min(poses[v].keypoints[parent].confidence,poses[v].keypoints[child].confidence))
                        J=np.array([[1.,0.,-p0[0]],[0.,1.,-p0[1]]])@self.relative[v]
                        focal=np.diag([k.fx,k.fy])
                        matrices.append(q*scales[v]*focal@J*self.length[side,part])
                        targets.append(q*focal@(p1-p0)[:2])
                    A=np.concatenate(matrices);target=np.concatenate(targets)
                    fitted=unit(np.linalg.solve(A.T@A,A.T@target))
                    before=A@original-target;after=A@fitted-target
                    e0=float(before@before);e1=float(after@after)
                    w=max(0.,(e0-e1)/(e0+e1)) if e0+e1 else 0.
                    direction=unit((1-w)*original+w*fitted)
                    info.update(weight=w,reason='current_geometry_weight',pixel_energy_before=e0,pixel_energy_fit=e1)
                except (ValueError,np.linalg.LinAlgError) as exc:direction=original;info['reason']=str(exc)
                directions.append(direction*self.flip.diagonal());weights.append(info['weight'])
                meta['arms'][side+'_'+part]=info
            if not directions or max(weights)==0:continue
            shoulder,elbow,wrist=(point(i) for i in (segments[0][2],segments[0][3],segments[1][3]))
            new_elbow=shoulder+np.linalg.norm(elbow-shoulder)*directions[0]
            new_wrist=new_elbow+np.linalg.norm(wrist-elbow)*directions[1]
            changed[segments[0][3]]=new_elbow;changed[segments[1][3]]=new_wrist
            for index in ((17,19,21) if side=='left' else (18,20,22)):
                changed[index]=point(index)+(new_wrist-wrist)
        if not changed:return primary,meta
        output=list(points)
        for index,value in changed.items():
            output[index]=MappingProxyType(dict(points[index],**dict(zip(('x','y','z'),map(float,value)))))
        result=object.__new__(WorldDisplayObservation)
        object.__setattr__(result,'source',primary.source)
        object.__setattr__(result,'world_landmarks',tuple(output))
        object.__setattr__(result,'normalized_landmarks',primary.normalized_landmarks)
        meta.update(active=True,modified_world_indices=sorted(changed))
        return result,meta


def install_two_view_camera_input(owner,profile=None):
    """One cold call. Missing/unknown configuration leaves the owner untouched."""
    receipt=dict(enabled=False,reason='camera_profile_disabled')
    if not isinstance(profile,Mapping) or profile.get('enabled') is not True:
        owner.two_view_input_receipt=receipt;return owner
    if profile.get('schema')!=SCHEMA:
        receipt['reason']='unknown_camera_profile_schema'
        owner.two_view_input_receipt=receipt;return owner
    try:
        if (profile['normalized_coordinates']!='rotated_full_frame' or
            profile['world_coordinates']!='primary_camera_xyz_y_down' or
            profile['minimum_q']!=.5):raise ValueError('unsupported_coordinate_contract')
        primary=profile['primary_avatar_view']
        if primary not in VIEWS or primary!=owner.view:raise ValueError('different_primary_avatar_view')
        geometry=profile['source_geometry']
        for v in VIEWS:
            g=geometry[v]
            if (type(g['width']) is not int or type(g['height']) is not int or min(g['width'],g['height'])<=0 or
                type(g['rotation_degrees']) is not int or g['rotation_degrees'] not in (0,90,180,270) or
                g['mirrored'] is not False):raise ValueError('unsupported_source_geometry')
        if owner.world_stabilizer is None:raise ValueError('existing_world_stabilizer_required')
        driver=TwoViewGeometryInput(profile['calibration'],owner.world.rig,
                                   primary_view=primary,source_geometry=geometry)
    except (KeyError,TypeError,ValueError) as exc:
        receipt['reason']='invalid_camera_profile: '+str(exc)
        owner.two_view_input_receipt=receipt;return owner
    prepare=owner.prepare;filter_apply=owner.world_stabilizer.apply;context={}
    def apply(observation):
        corrected,metadata=driver.apply(observation,context['work']);context['metadata']=metadata
        return filter_apply(corrected)
    def wrapped(work):
        context['work']=work
        try:
            result=prepare(work);result['two_view_geometry_input']=context.get('metadata',dict(active=False))
            return result
        finally:context.clear()
    owner.world_stabilizer.apply=apply;owner.prepare=wrapped
    owner.two_view_input_driver=driver
    owner.two_view_input_receipt=dict(enabled=True,profile_id=profile.get('profile_id'),
        primary_avatar_view=primary,source_geometry={v:list(g) for v,g in driver.expected_geometry.items()},
        existing_filter=type(owner.world_stabilizer).__name__,added_temporal_filter=False)
    return owner
