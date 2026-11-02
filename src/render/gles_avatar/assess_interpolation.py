"""Host-only full-mesh interpolation assessment; never opens a board or camera."""
import argparse,sys,json,collections,time
from pathlib import Path
from contextlib import closing
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts'),str(Path(__file__).resolve().parent)]
from posepriv_coach.avatar_setup import load_avatar_preview_setup
from posepriv_coach.pose_avatar_preview import PoseAvatarPreviewPlanner
from posepriv_coach.preview_continuity import PreviewMeshContinuity
from posepriv_coach.preview_limb_continuity import PreviewLimbContinuity
from posepriv_coach.avatar_skinning import _pack_skinned_fields_from_bank
from replay_sources import load_playlist
from mesh_interpolation import DelayedMeshInterpolation,reference_positions,basis_array
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--playlist',type=Path,required=True)
parser.add_argument('--mesh',type=Path,required=True)
parser.add_argument('--reference',type=Path,required=True,help='private original display-clock reference')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--oracle-frame',action='append',default=[],help='chapter:local index for full scalar byte check')
args=parser.parse_args()
if args.output.exists():parser.error('preserve existing assessment; choose a new output')
checks={tuple(map(int,item.split(':'))) for item in args.oracle_frame} or {(0,0)}
chapters=load_playlist(args.playlist,fps=30)
if any(len(item)!=2 or item[0]<0 or item[0]>=len(chapters) or not chapters[item[0]].raw_pairs or item[1]<0 or item[1]>=len(chapters[item[0]].samples) for item in checks):parser.error('oracle-frame must select an available pose frame')
ref=json.loads(args.reference.read_text());summaries=[];globalframe=0;scalar_checks=0
if sum(len(c.samples) for c in chapters)!=len(ref['timings']):parser.error('reference and playlist lengths differ')

def stats(v):
 return dict(count=len(v),mean=float(np.mean(v)),p95=float(np.percentile(v,95)),maximum=float(np.max(v))) if v else None

with closing(load_avatar_preview_setup(mesh_path=args.mesh)) as setup:
 asset=setup.mesh_asset;r=setup.runtime
 vertices=np.array([(v.position.x,v.position.y,v.position.z) for v in asset.vertices],np.int64)
 ids=np.array([v.matrix_indices for v in asset.vertices],np.intp);weights=np.array([v.weights_q0_15 for v in asset.vertices],np.int64)
 def quant(v,shift):return np.where(v<0,-1,1)*((np.abs(v)+(1<<(shift-1)))>>shift)
 def skin(plan):
  mat=np.array([w.matrix.values for w in plan.matrix_writes],np.int64).reshape(-1,3,4);acc=np.zeros(vertices.shape,np.int64)
  for lane in range(4):
   m=mat[ids[:,lane]]
   raw=m[:,:,0]*vertices[:,0,None]+m[:,:,1]*vertices[:,1,None]+m[:,:,2]*vertices[:,2,None]+(m[:,:,3]<<14)
   transformed=np.clip(quant(raw,14),-131072,131071)
   acc+=transformed*weights[:,lane,None]
  output=np.zeros((len(vertices),8),dtype='<i2');output[:,:3]=np.clip(quant(acc,15),-32768,32767)
  return output.tobytes(),output[:,:3].astype(float)/16384.
 for ci,c in enumerate(chapters):
  if not c.raw_pairs:globalframe+=len(c.samples);continue
  planner=PoseAvatarPreviewPlanner(hand_bones=True,depth_stability=True)
  bones=PreviewMeshContinuity([planner.config.bone_links[i] for i in r.bone_topology_indices]);limbs=PreviewLimbContinuity(bones.slot_links);interpolator=DelayedMeshInterpolation()
  reasons=collections.Counter();rms=[[],[]];accel=[[],[]];lag=[];normratio=[];cost=[];previous=None;before_previous=None;interpolated=0;sameposes=[0,0];maxhistory=0
  for li,s in enumerate(c.samples):
   observed=planner.build(s.pair,vertex_requests=r.vertex_requests,bone_bind_lengths_q1_14=asset.bone_bind_lengths_q1_14,inverse_bind_matrices_q1_14=asset.inverse_bind_matrices_q1_14,bone_topology_indices=r.bone_topology_indices)
   base=bones.apply(observed);limb=limbs.apply(base,display_timestamp_ns=s.display_timestamp_ns);plan=limb.render_plan
   expected=ref['timings'][globalframe]
   assert {v:row.frame_id for v,row in s.rows.items()}==expected['source_ids']
   assert s.display_timestamp_ns==expected['display_timestamp_ns'] and s.observation_ready_ns==expected['observation_ready_ns']
   assert list(plan.matrix_valid)==expected['render_matrix_valid'] and list(limb.held_slots)==expected['held_slots']
   payload,original=skin(plan)
   if (ci,li) in checks:
    bank=tuple(w.matrix for w in plan.matrix_writes)
    scalar=b''.join(_pack_skinned_fields_from_bank(*map(int,v),tuple(map(int,idx)),tuple(map(int,w)),bank)+b'\0'*8 for v,idx,w in zip(vertices,ids,weights))
    assert payload==scalar;scalar_checks+=1
   started=time.perf_counter_ns();selection=interpolator.select(payload,plan.matrix_writes,plan.matrix_valid,ready_ns=s.observation_ready_ns,display_ns=s.display_timestamp_ns,held=bool(limb.held_slots));cost.append(time.perf_counter_ns()-started)
   current=reference_positions(selection);reasons[selection.reason]+=1;interpolated+=selection.interpolated;maxhistory=max(maxhistory,interpolator.history_size)
   if selection.interpolated:
    assert selection.left.ready_ns<=selection.target_ns<=selection.right.ready_ns<=s.display_timestamp_ns
    a=basis_array(selection.left.matrices)[list(plan.matrix_valid)];b=basis_array(selection.right.matrices)[list(plan.matrix_valid)];f=selection.fraction
    normratio.append(float(np.min(np.linalg.norm(a+(b-a)*f,axis=1)/((1-f)*np.linalg.norm(a,axis=1)+f*np.linalg.norm(b,axis=1)))))
   visible=(np.asarray(plan.matrix_valid)[ids]|(weights==0)).all(axis=1)
   if np.any(visible):lag.append(float(np.max(np.linalg.norm((current-original)[visible],axis=1))))
   if previous is not None:
    mask=visible&previous[2]
    if np.any(mask):
     for lane,xyz in enumerate((original,current)):
      delta=(xyz-previous[lane])[mask]
      rms[lane].append(float(np.sqrt(np.mean(np.sum(delta*delta,axis=1)))))
      sameposes[lane]+=bool(np.all(delta==0))
    if before_previous is not None:
     mask=mask&before_previous[2]
     if np.any(mask):
      for lane,xyz in enumerate((original,current)):
       delta=(xyz-2*previous[lane]+before_previous[lane])[mask]
       accel[lane].append(float(np.sqrt(np.mean(np.sum(delta*delta,axis=1)))))
   before_previous=previous;previous=(original,current,visible);globalframe+=1
  summaries.append(dict(chapter=ci,frames=len(c.samples),reasons=dict(reasons),interpolated_frames=interpolated,max_history=maxhistory,selector_host_ns=stats(cost),baseline_motion_rms=stats(rms[0]),candidate_motion_rms=stats(rms[1]),baseline_acceleration_rms=stats(accel[0]),candidate_acceleration_rms=stats(accel[1]),unchanged_visible_position_frames=sameposes,visible_max_residual=stats(lag),minimum_basis_column_length_ratio=min(normratio,default=None)))
  print(json.dumps(summaries[-1],indent=2),flush=True)
result=dict(scope='host pose-chapter mesh reference prototype; no GPU/FPGA/FPS claim; original pose sources/valid/held match reference',pose_frames=sum(c['frames'] for c in summaries),reference_playlist_frames=len(ref['timings']),raw_chapters_skipped=True,display_delay_ns=50_000_000,scalar_full_mesh_checks=scalar_checks,chapters=summaries)
assert scalar_checks==len(checks)
args.output.parent.mkdir(parents=True,exist_ok=True)
with args.output.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
