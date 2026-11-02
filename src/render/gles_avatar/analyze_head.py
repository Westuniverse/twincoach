"""Aggregate displayed head rotations; no human-angle truth or FPS inference."""
import argparse
import json
import math
from pathlib import Path

import numpy as np


def distribution(values):
    return (dict(count=len(values),p95=float(np.percentile(values,95)),maximum=max(values),
                 over20=sum(v>20 for v in values)) if values else None)


def head_statistics(rows,*,head_slot):
    result=[];chapter=None;previous=None;last_valid=None;state=None
    for row in rows:
        if row['chapter']!=chapter:
            if state is not None:result.append(state)
            chapter=row['chapter'];previous=last_valid=None
            state=dict(chapter=chapter,frames=0,valid_frames=0,observed_valid_frames=0,
                       hidden_matrix_frames=0,
                       adjacent=[],continuous_observed=[],long_source_gap=[],reacquired=[],target_error=[])
        state['frames']+=1
        values=row.get('head_matrix_q1_14',())
        if values and not row.get('avatar_visible',True):
            state['hidden_matrix_frames']+=1
            values=()
        time=row['display_timestamp_ns'];ready=row['observation_ready_ns']
        observed=bool(row['observed_matrix_valid'] and row['observed_matrix_valid'][head_slot]
                      and head_slot not in row['held_slots'])
        if previous is not None and (time<=previous[0] or ready<previous[1]):
            raise ValueError('head timestamps must advance within each chapter')
        rotation=None
        if values:
            if len(values)!=12:raise ValueError('head affine matrix needs 12 coefficients')
            matrix=np.asarray(values,dtype=float).reshape(3,4)[:,:3]/16384.
            u,s,vt=np.linalg.svd(matrix)
            if min(s)<1.e-8 or np.linalg.det(u@vt)<0:raise ValueError('head matrix must have a proper nondegenerate rotation')
            rotation=u@vt;state['valid_frames']+=1;state['observed_valid_frames']+=observed
            def angle(other):
                cosine=float(np.clip((np.trace(rotation@other.T)-1)/2,-1,1))
                return math.degrees(math.acos(cosine))
            if previous is not None and previous[2] is not None:
                value=angle(previous[2]);state['adjacent'].append(value)
                delta=ready-previous[1]
                if observed and previous[3] and 0<delta<=100_000_000:state['continuous_observed'].append(value)
                if delta>100_000_000:state['long_source_gap'].append(value)
            elif last_valid is not None:state['reacquired'].append(angle(last_valid))
            if observed and row.get('head_target_error_rad') is not None:
                value=row['head_target_error_rad']
                if not math.isfinite(value) or value<0:raise ValueError('finite nonnegative target error required')
                state['target_error'].append(math.degrees(value))
            last_valid=rotation
        previous=time,ready,rotation,observed
    if state is not None:result.append(state)
    return [dict(**{k:v for k,v in state.items() if not isinstance(v,list)},
                 **{name+'_degrees':distribution(state[name]) for name in
                    ('adjacent','continuous_observed','long_source_gap','reacquired','target_error')}) for state in result]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--head-slot',type=int,required=True)
    args=parser.parse_args()
    report=json.loads(args.report.read_text())
    if args.head_slot<0:parser.error('head-slot must be nonnegative')
    result=dict(scope=__doc__,head_slot=args.head_slot,
                continuous_scope='both endpoints observed valid, not held; actual ready gap >0 and <=100ms',
                clock_scope='adjacent displayed render-valid matrices; source gaps/reacquisition reported separately; no angular-rate proof',
                chapters=head_statistics(report['timings'],head_slot=args.head_slot))
    with args.output.open('x') as out:out.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
