"""Explicit precomputed image-only palettes for the PPS4/GLES experiment.

No model inference or scoring: the original scoring sample stays with its own
chapter. Only the image display schedule and render plan come from this cache.
"""
from dataclasses import replace
import json
from pathlib import Path

from posepriv_coach.avatar_image_source import (AvatarImageSource,DisplayImageKeypoints,
    ImageAvatarFramePlan)
from posepriv_coach.avatar_skinning import AffineMatrixQ1_14,MatrixWrite
from posepriv_coach.contracts import Keypoint
from posepriv_coach.preview_rigid_avatar import RigidAvatarPreview
from replay_sources import latest_image_samples

WORLD_DISPLAY_STATUS_ZH='三维推断 · 录像跟随'
WORLD_SCORE_SOURCE_ZH='评分来自原双摄记录'


class WorldAvatarCache:
    def __init__(self,path,chapters,asset,vertex_requests):
        import numpy as np
        path=Path(path).resolve(strict=True)
        manifest=json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('schema_version')!=1 or len(manifest.get('chapters',[]))!=len(chapters):
            raise ValueError('world cache schema/playlist chapter count differ')
        binding=dict(bone_names=list(asset.bone_names),lengths=list(asset.bone_bind_lengths_q1_14),
                     inverse_bind=[list(m.values)for m in asset.inverse_bind_matrices_q1_14])
        self.chapters=tuple(chapters);self.requests=tuple(vertex_requests)
        self.entries=[];self.samples=[];self.folders=[]
        for chapter,entry in zip(chapters,manifest['chapters']):
            self.samples.append(latest_image_samples(chapter))
            if entry is None:self.entries.append(None);self.folders.append(None);continue
            folder=Path(entry)
            if not folder.is_absolute():folder=path.parent/folder
            self.folders.append(folder)
            report=json.loads((folder/'report.json').read_text(encoding='utf-8'))
            if (report.get('schema_version')!=1 or
                    report.get('scope')!='host_image_world_avatar_preview' or
                    report.get('source_schedule')!='latest-raw' or
                    not report.get('all_geometry_inferred') or
                    report.get('frames')!=len(chapter.samples) or report.get('asset_binding')!=binding):
                raise ValueError('world cache source/asset binding differs')
            states=json.loads((folder/'states.json').read_text(encoding='utf-8'))
            n=len(chapter.samples)
            if len(states)!=n:raise ValueError('world cache state count differs')
            matrices=np.frombuffer((folder/'matrices.bin').read_bytes(),dtype='<i2').reshape(n,16,12)
            displays=np.frombuffer((folder/'display.bin').read_bytes(),dtype='<f4').reshape(n,16)
            if not np.isfinite(displays).all()or not np.array_equal(
                    displays[:,12:],np.tile((0.,0.,0.,1.),(n,1))):
                raise ValueError('world cache display affine is invalid')
            images=[];sources=[]
            for state,sample in zip(states,self.samples[-1]):
                source=AvatarImageSource(**state['source']);row=sample.rows.get(source.view)
                if (row is None or (row.frame_id,row.timestamp_ns)!=(source.frame_id,source.timestamp_ns)or
                        chapter.rotation_degrees[source.view]!=source.rotation_degrees or
                        sample.display_timestamp_ns!=state['display_timestamp_ns']):
                    raise ValueError('world cache and causal original image differ')
                valid=state['matrix_valid']
                if (tuple(state['inferred_slots'])!=tuple(range(13))or len(valid)!=16 or
                        any(type(v)is not bool for v in valid)or
                        state['reason']not in('partial','held_body','neutral','recorded_hold')):
                    raise ValueError('world cache must retain complete inferred display labels')
                markers=DisplayImageKeypoints(source,{n:Keypoint(*p)for n,p in state.get('image_keypoints',{}).items()})
                images.append(replace(sample,display_keypoints={source.view:markers}));sources.append(source)
            self.samples[-1]=tuple(images)
            self.entries.append((states,matrices,displays,tuple(sources)))

    def frame(self,chapter_slot,index):
        entry=self.entries[chapter_slot]
        if entry is None:return None
        states,matrices,displays,sources=entry;state=states[index]
        source=sources[index];inferred=tuple(state['inferred_slots']);valid=tuple(state['matrix_valid'])
        plan=ImageAvatarFramePlan(source,{},(),tuple(MatrixWrite(i,AffineMatrixQ1_14(
            tuple(int(v)for v in matrix)))for i,matrix in enumerate(matrices[index])),valid,self.requests)
        return RigidAvatarPreview(plan,inferred,state['reason'],state['body_scale'],state['head_mode'],
            display_transform=tuple(float(v)for v in displays[index]),
            display_body_scale=state['display_body_scale'])
