# SPDX-License-Identifier: GPL-2.0-only
"""Private current-frame torso orientation, before the existing single filter."""
from types import MappingProxyType
import numpy as np
from posepriv_coach.two_view_display_geometry import TwoViewGeometryInput,unit
from posepriv_coach.world_avatar_preview import WorldDisplayObservation


class TwoViewTorsoHeadTranslationInput(TwoViewGeometryInput):
    def apply(self,primary,work):
        info=dict(active=False)
        output=primary
        if primary.world_landmarks:
            try:
                torso={}
                for v,geometry in self.expected_geometry.items():
                    source=work['world_observations'][v].source
                    if (source.view,source.width,source.height,source.rotation_degrees)!=geometry:
                        raise ValueError('different_camera_geometry')
                    p=work['poses'][v].keypoints
                    names=('left_shoulder','right_shoulder','left_hip','right_hip')
                    if any(n not in p or p[n].confidence<.5 for n in names):raise ValueError('q_below_0p5')
                    if any(not(0<=p[n].x<=1 and 0<=p[n].y<=1)for n in names):raise ValueError('outside_source')
                    rays={n:self.ray(v,p[n])for n in names}
                    torso[v]=((rays[names[0]]+rays[names[1]])/2,(rays[names[2]]+rays[names[3]])/2)
                normals={v:unit(self.R[v]@np.cross(h,s))for v,(s,h)in torso.items()}
                target=unit(np.cross(normals['front'],normals['side']))
                signs=[]
                for v,(s,h)in torso.items():
                    a=self.R[v].T@target
                    signs.append(float((a[:2]-h[:2]*a[2])@(s-h)[:2]))
                if signs[0]*signs[1]<=0:raise ValueError('torso_direction_conflict')
                if signs[0]<0:target=-target
                target=self.R[self.primary_view].T@target
                points=primary.world_landmarks
                p=np.array([[q[k]for k in ('x','y','z')]for q in points])
                pivot=(p[23]+p[24])/2
                original=unit((p[11]+p[12])/2-pivot)
                cross=np.cross(original,target);c=float(original@target)
                if c<-.999999:raise ValueError('opposite_torso_axes')
                K=np.array([[0.,-cross[2],cross[1]],[cross[2],0.,-cross[0]],[-cross[1],cross[0],0.]])
                rotation=np.eye(3)+K+K@K/(1+c)
                transformed=(p[:23]-pivot)@rotation.T+pivot
                # Same torso rotation, but head/face keeps its observed relative direction.
                shoulder_delta=(transformed[11]+transformed[12]-p[11]-p[12])/2
                transformed[:11]=p[:11]+shoulder_delta
                corrected=list(points)
                for i,value in enumerate(transformed):
                    corrected[i]=MappingProxyType(dict(points[i],**dict(zip(('x','y','z'),map(float,value)))))
                output=object.__new__(WorldDisplayObservation)
                object.__setattr__(output,'source',primary.source)
                object.__setattr__(output,'world_landmarks',tuple(corrected))
                object.__setattr__(output,'normalized_landmarks',primary.normalized_landmarks)
                info.update(active=True,angle_deg=float(np.degrees(np.arccos(np.clip(c,-1.,1.)))),
                    rotation=rotation.tolist(),pivot=pivot.tolist(),head_translation=shoulder_delta.tolist())
            except (ValueError,np.linalg.LinAlgError)as error:info['reason']=str(error)
        # The public cold installer retains its existing driver instance; this
        # private method is bound onto it for the one source0 experiment.
        output,metadata=TwoViewGeometryInput.apply(self,output,work)
        metadata['torso_current_geometry']=info
        if info['active']:
            metadata['active']=True
            metadata['modified_world_indices']=sorted(set(metadata.get('modified_world_indices',()))|set(range(23)))
        return output,metadata
