"""Full same-source host head-target comparison; no board or pose-truth claim."""
import argparse
from collections import Counter
from contextlib import closing
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from posepriv_coach.avatar_setup import load_avatar_preview_setup
from posepriv_coach.pose_avatar_preview import PoseAvatarPreviewPlanner
from posepriv_coach.preview_continuity import PreviewMeshContinuity
from posepriv_coach.preview_limb_continuity import PreviewLimbContinuity
from posepriv_coach.display import completed_score_text
from replay_sources import load_playlist, ReplayScoring
from analyze_head import head_statistics


def assess(playlist,mesh):
    import numpy as np
    chapters=load_playlist(playlist,fps=30)
    records={mode:[] for mode in ('flat','depth','face')}
    score_changes=[];scorer=ReplayScoring(ROOT/'config/exercises/squat_depth_angle_front_order_v1.json')
    summaries=[]
    with closing(load_avatar_preview_setup(mesh_path=mesh)) as setup:
        asset=setup.mesh_asset;runtime=setup.runtime
        indices=np.asarray([v.matrix_indices for v in asset.vertices]);zero=np.asarray([v.weights_q0_15 for v in asset.vertices])==0
        faces=np.asarray(asset.triangles)
        kw=dict(vertex_requests=runtime.vertex_requests,bone_bind_lengths_q1_14=asset.bone_bind_lengths_q1_14,
            inverse_bind_matrices_q1_14=asset.inverse_bind_matrices_q1_14,bone_topology_indices=runtime.bone_topology_indices)
        for ci,ch in enumerate(chapters):
            options=dict(hand_bones=True,depth_stability=True,core_view_support=True)
            planners=dict(flat=PoseAvatarPreviewPlanner(**options),
                depth=PoseAvatarPreviewPlanner(**options,head_depth=True,head_stability=True),
                face=PoseAvatarPreviewPlanner(**options,head_face_axis=True,head_stability=True))
            bones={m:PreviewMeshContinuity([p.config.bone_links[i] for i in runtime.bone_topology_indices]) for m,p in planners.items()}
            limbs={m:PreviewLimbContinuity(b.slot_links) for m,b in bones.items()}
            counts=Counter();max_age=0
            for li,s in enumerate(ch.samples):
                state=scorer.update(ci,ch,s)
                if s.pair is not None:
                    key=[state.completed_repetition_count,completed_score_text(state)]
                    if not score_changes or score_changes[-1]['chapter']!=ci or score_changes[-1]['value']!=key:
                        score_changes.append(dict(frame=len(records['flat']),chapter=ci,value=key))
                original=None
                for mode,planner in planners.items():
                    row=dict(chapter=ci,local_frame=li,chapter_kind=ch.kind,
                        source_ids={v:idx.frame_id for v,idx in s.rows.items()},display_timestamp_ns=s.display_timestamp_ns,
                        observation_ready_ns=s.observation_ready_ns,observed_matrix_valid=(),render_matrix_valid=(),held_slots=(),
                        limb_held_slots=(),limb_suppressed_slots=(),limb_hold_ages_ns=(),limb_reasons=(),
                        triangles=0,planned_triangles=0,avatar_visible=False,head_matrix_q1_14=(),head_target_error_rad=None,
                        head_face_axis_reason=None,head_face_axis_sides=(),head_face_axis_age_ns=None,head_face_axis_pitch_rad=None)
                    if s.pair is not None:
                        observed=planner.build(s.pair,display_timestamp_ns=s.display_timestamp_ns,**kw)
                        if original is None:original=observed
                        else:
                            assert observed.pair is original.pair is s.pair
                            assert {k:v for k,v in original.joints.items() if k!='nose'}=={k:v for k,v in observed.joints.items() if k!='nose'}
                            assert original.segments[:2]+original.segments[3:]==observed.segments[:2]+observed.segments[3:]
                            assert original.matrix_writes[:2]+original.matrix_writes[3:]==observed.matrix_writes[:2]+observed.matrix_writes[3:]
                            assert original.matrix_valid==observed.matrix_valid
                        frame=limbs[mode].apply(bones[mode].apply(observed),display_timestamp_ns=s.display_timestamp_ns);plan=frame.render_plan
                        visible=(np.asarray(plan.matrix_valid)[indices]|zero).all(axis=1)
                        triangles=int(visible[faces].all(axis=1).sum());show=all(plan.matrix_valid[:3])
                        row.update(observed_matrix_valid=observed.matrix_valid,render_matrix_valid=plan.matrix_valid,
                            held_slots=frame.held_slots,limb_held_slots=frame.chain_held_slots,
                            limb_suppressed_slots=frame.suppressed_slots,limb_hold_ages_ns=frame.hold_ages_ns,limb_reasons=frame.reasons,
                            planned_triangles=triangles,triangles=triangles if show else 0,avatar_visible=show and triangles>0,
                            head_matrix_q1_14=plan.matrix_writes[2].matrix.values if plan.matrix_valid[2] else (),
                            head_target_error_rad=planner.head_tracker.last_target_error_rad if planner.head_tracker else None)
                        status=planner.last_head_face_axis
                        if status is not None:
                            row.update(head_face_axis_reason=status.reason,head_face_axis_sides=status.sides,
                                head_face_axis_age_ns=status.source_age_ns,head_face_axis_pitch_rad=status.pitch_rad)
                            counts[status.reason]+=1
                            if status.sides:
                                assert status.source_age_ns==s.display_timestamp_ns-s.observation_ready_ns<=100_000_000
                                max_age=max(max_age,status.source_age_ns)
                        nose=observed.joints.get('nose');neck=observed.joints.get('neck')
                        if nose and neck:
                            row['head_axis_pitch_rad']=math.atan2(nose.position[2]-neck.position[2],nose.position[1]-neck.position[1])
                    records[mode].append(row)
            summary=dict(chapter=ci,frames=len(ch.samples),kind=ch.kind,face_support_reasons=dict(counts),max_face_support_age_ns=max_age)
            summaries.append(summary);print(json.dumps(summary),flush=True)
    return dict(scope=__doc__,frames=len(records['flat']),non_head_fields_equal=True,
        chapters=summaries,score_changes=score_changes,
        modes={mode:dict(head=head_statistics(rows,head_slot=2),timings=rows) for mode,rows in records.items()})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--playlist',type=Path,required=True);parser.add_argument('--mesh',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('output already exists')
    result=assess(args.playlist,args.mesh)
    with args.output.open('x') as f:json.dump(result,f)
    print(json.dumps({m:v['head'][:2] for m,v in result['modes'].items()},indent=2))


if __name__=='__main__':main()
