"""Offline analysis of host and grouped GPU completion timestamps.

The input is the explicit GLES experiment report. A host return is never treated
as a physical scanout timestamp or an individual asynchronous GPU completion.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from statistics import mean, median


STAGES = ('previous_gpu_finish_wait_ns', 'score_ns', 'score_wait_ns', 'score_worker_ns', 'late_score_ns', 'plan_ns', 'skin_ns', 'prepare_and_decode_ns',
          'gpu_upload_draw_finish_ns', 'mesh_rows_ns')


def _integer(value, label, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{label} must be an integer >= {minimum}')
    return value


def _statistics(values):
    ordered = sorted(values)
    if not ordered:
        return None
    def percentile(p):
        return ordered[math.ceil(p*len(ordered))-1]
    return dict(count=len(ordered), mean=mean(ordered), median=median(ordered),
                p95=percentile(.95), p99=percentile(.99), maximum=ordered[-1])


def _one_second_completion_bounds(drains, target_fps, saved):
    """Bound (T-1s, T] counts using only certified cumulative drain counts."""
    times=[row['finished_ns'] for _,row in drains]
    counts=[row['completed_count'] for _,row in drains]
    diagnostic_times=[row['finished_ns'] for i,row in drains if i in saved]
    windows=[]
    for (frame,_),right_time,right_count in zip(drains,times,counts):
        left_time=right_time-1_000_000_000
        if left_time<times[0]:
            continue
        index=bisect_left(times,left_time)
        exact_left=times[index]==left_time
        if exact_left:
            lower=upper=right_count-counts[index]
        else:
            # C(a) is between the previous and next certified cumulative count.
            # Subtraction reverses those bounds; group-internal times are unknown.
            lower=right_count-counts[index]
            upper=right_count-counts[index-1]
        status=('known_failure' if upper<target_fps else
                'proven' if lower>=target_fps else 'uncertain')
        windows.append(dict(right_frame=frame,
            right_seconds_from_first_warm_drain=(right_time-times[0])/1e9,
            completed_frames_lower=lower,completed_frames_upper=upper,
            left_boundary_exact=exact_left,status=status,
            diagnostic_snapshot_in_window=any(left_time<t<=right_time for t in diagnostic_times)))
    failures=sum(w['status']=='known_failure' for w in windows)
    unknown=sum(w['status']=='uncertain' for w in windows)
    proven=sum(w['status']=='proven' for w in windows)
    return dict(window_seconds=1.,right_endpoints='warm GPU-drain endpoints only',
        complete_window_count=len(windows),minimum_completed_frames_lower=min((w['completed_frames_lower'] for w in windows),default=None),
        minimum_completed_frames_upper=min((w['completed_frames_upper'] for w in windows),default=None),
        known_below_target_windows=failures,uncertain_windows=unknown,proven_at_least_target_windows=proven,
        all_sampled_windows_proven=(failures==0 and unknown==0) if windows else None,
        status=('insufficient_history' if not windows else 'known_failure' if failures else
                'uncertain' if unknown else 'proven_for_sampled_windows'),
        diagnostic_windows_retained=True,individual_gpu_times_interpolated=False,
        physical_scanout_measured=False,all_continuous_right_endpoints_evaluated=False,
        worst_sampled_windows=sorted(windows,key=lambda w:(w['completed_frames_upper'],w['completed_frames_lower'],w['right_frame']))[:8])


def _chapter_completion(rows, drains, saved, warmup, target_fps):
    if any(type(row.get('chapter')) is not int for row in rows):
        return dict(available=False, reason='complete internal chapter indices unavailable', runs=[])
    boundaries=[0]+[i for i in range(1,len(rows)) if rows[i]['chapter']!=rows[i-1]['chapter']]+[len(rows)]
    runs=[]
    for start,end in zip(boundaries,boundaries[1:]):
        warm_start=max(start,warmup)
        endpoints=[(i,row) for i,row in drains if warm_start<=i<end]
        count=(endpoints[-1][1]['completed_count']-endpoints[0][1]['completed_count']
               if len(endpoints)>1 else 0)
        seconds=((endpoints[-1][1]['finished_ns']-endpoints[0][1]['finished_ns'])/1e9
                 if len(endpoints)>1 else 0.)
        fps=count/seconds if seconds else None
        kinds={row.get('chapter_kind') if isinstance(row.get('chapter_kind'),str) else None
               for row in rows[start:end]}
        kind=next(iter(kinds)) if len(kinds)==1 else None
        runs.append(dict(chapter=rows[start]['chapter'],kind=kind if kind in ('pose_trace','raw_indexed') else None,
            first_frame=start,last_frame=end-1,frames=end-start,
            warm_frames=max(0,end-warm_start),global_warmup_only=True,
            first_frame_work_ms=rows[start]['total_ns']/1e6,
            warm_host_work_ms=_statistics([row['total_ns']/1e6 for row in rows[warm_start:end]]),
            frames_between_certified_endpoints=count,seconds_between_certified_endpoints=seconds,
            gpu_completed_fps=fps,average_target_met=fps>=target_fps if fps is not None else None,
            frames_through_first_warm_drain=(endpoints[0][0]-warm_start+1 if endpoints else max(0,end-warm_start)),
            frames_after_last_warm_drain=(end-1-endpoints[-1][0] if endpoints else 0),
            drain_anchored_rolling_windows=_one_second_completion_bounds(endpoints,target_fps,saved)))
    return dict(available=True,scope='contiguous chapters; only internal certified drain endpoints; global boundary costs remain in overall statistics',runs=runs)


def _completion_drains(report,rows,warmup):
    events=report.get('gpu_completion_events')
    if events is None:
        if report.get('late_finish'):
            raise ValueError('late finish requires per-frame completion certificates')
        return [(i,row) for i,row in enumerate(rows) if i>=warmup and row['gpu_drained']]
    if (not report.get('late_finish') or report.get('gpu_completion_event_schema')!='posepriv.gles.completed_frame.v1'
            or not isinstance(events,list) or len(events)!=len(rows)):
        raise ValueError('one valid completion certificate per generated frame required')
    previous=None
    for n,(event,row) in enumerate(zip(events,rows)):
        if (not isinstance(event,dict) or type(event.get('frame_index')) is not int or event['frame_index']!=n
                or type(event.get('completed_count')) is not int or event['completed_count']!=n+1):
            raise ValueError('completion certificate frame/count mismatch')
        commands=_integer(row.get('gpu_commands_started_ns'),'gpu_commands_started_ns')
        submitted=_integer(row.get('gpu_submitted_ns'),'gpu_submitted_ns')
        drained=_integer(event.get('drained_ns'),'certificate drained_ns')
        completed=_integer(event.get('finished_ns'),'certificate finished_ns')
        if not row['started_ns']<=commands<=submitted<=row['finished_ns']:
            raise ValueError('GPU command markers outside host loop')
        if drained<submitted or completed<drained or completed>rows[-1]['finished_ns'] or previous is not None and completed<=previous:
            raise ValueError('completion certificate reverses or precedes submission')
        if n+1<len(rows) and completed>_integer(rows[n+1].get('gpu_commands_started_ns'),'next gpu_commands_started_ns'):
            raise ValueError('next GPU frame started before prior completion certificate')
        previous=completed
    return [(n,event) for n,event in enumerate(events) if n>=warmup]


def analyze_generation_timing(report, *, target_fps=15.0):
    """Return privacy-safe aggregates, retaining all measured warm intervals.

    Completion timestamps denote a host-observed ``glFinish`` drain followed by
    optional readback/swap. They certify cumulative finished GPU work, but do not
    locate each frame's completion within a multi-frame drain group.
    """
    if isinstance(target_fps, bool) or not isinstance(target_fps, (float, int)) or not math.isfinite(target_fps) or target_fps <= 0:
        raise ValueError('positive finite target_fps required')
    rows = report.get('timings')
    if not isinstance(rows, list) or not rows:
        raise ValueError('nonempty timing rows required')
    if _integer(report.get('frames'), 'frames', minimum=1) != len(rows):
        raise ValueError('reported frame count differs from timing rows')
    warmup = _integer(report.get('warmup_frames', 0), 'warmup_frames')
    if warmup >= len(rows):
        raise ValueError('warmup leaves no measured frames')
    saved = report.get('saved_frames', [])
    if not isinstance(saved, list) or any(type(n) is not int or not 0 <= n < len(rows) for n in saved):
        raise ValueError('saved_frames must contain valid internal frame indices')
    saved = set(saved)
    score_changes = report.get('score_changes') or []
    if not isinstance(score_changes, list) or any(not isinstance(row, dict) or
            type(row.get('frame')) is not int or not 0 <= row['frame'] < len(rows) for row in score_changes):
        raise ValueError('score_changes must contain valid internal frame indices')
    score_change_frames = {row['frame'] for row in score_changes}
    previous_finish = None
    for n, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError('timing rows must be mappings')
        started = _integer(row.get('started_ns'), 'started_ns')
        finished = _integer(row.get('finished_ns'), 'finished_ns')
        if finished < started or previous_finish is not None and started < previous_finish:
            raise ValueError('timing rows overlap or reverse host order')
        if _integer(row.get('total_ns'), 'total_ns') != finished-started:
            raise ValueError('total_ns differs from host start/finish')
        if type(row.get('gpu_drained')) is not bool:
            raise ValueError('gpu_drained must be boolean')
        if row['gpu_drained']:
            if type(row.get('completed_count')) is not int or row['completed_count'] != n+1:
                raise ValueError('drain completed_count must equal cumulative frame count')
        elif row.get('completed_count') is not None:
            raise ValueError('undrained row cannot certify a completion count')
        for key in STAGES:
            if key in row:
                _integer(row[key], key)
        previous_finish = finished
    if not rows[-1]['gpu_drained']:
        raise ValueError('final frame must drain outstanding GPU work')

    warm = rows[warmup:]
    # Same two certified endpoints as the board report's grouped FPS calculation.
    drains = _completion_drains(report,rows,warmup)
    groups = []
    for (left_i, left), (right_i, right) in zip(drains, drains[1:]):
        elapsed = right['finished_ns']-left['finished_ns']
        if elapsed <= 0:
            raise ValueError('drain completion timestamps must advance')
        frames = right['completed_count']-left['completed_count']
        groups.append(dict(last_frame=right_i, frames=frames, interval_ms=elapsed/1e6,
                           amortized_ms_per_frame=elapsed/frames/1e6,
                           diagnostic_readback=any(j in saved for j in range(left_i+1, right_i+1)),
                           after_diagnostic=left_i in saved))
    completed_frames = sum(g['frames'] for g in groups)
    completion_seconds = sum(g['interval_ms'] for g in groups)/1000
    completed_fps = completed_frames/completion_seconds if groups else None
    budget_ms = 1000/target_fps
    intervals = []
    for n in range(warmup+1, len(rows)):
        current, previous = rows[n], rows[n-1]
        interval = (current['finished_ns']-previous['finished_ns'])/1e6
        gap = (current['started_ns']-previous['finished_ns'])/1e6
        intervals.append(dict(frame=n, interval_ms=interval, interframe_gap_ms=gap,
                              work_ms=current['total_ns']/1e6,
                              diagnostic_readback=n in saved, after_diagnostic=n-1 in saved,
                              score_change=n in score_change_frames,
                              chapter_change=('chapter' in current and current['chapter'] != previous.get('chapter')),
                              stages_ms={key: current[key]/1e6 for key in STAGES if key in current}))
    stage_statistics = {key[:-3]+'_ms': _statistics([row[key]/1e6 for row in warm if key in row]) for key in STAGES}
    stage_statistics = {key: value for key, value in stage_statistics.items() if value is not None}
    diagnostic_gaps = [row['interframe_gap_ms'] for row in intervals if row['after_diagnostic']]
    score_timing_key='score_worker_ns' if report.get('score_process') else 'score_ns'
    score_frames = [(n, row[score_timing_key]/1e6) for n, row in enumerate(rows)
                    if n >= warmup and score_timing_key in row]
    heavy_score_frames = [n for n, value in score_frames if value > 20.]
    result=dict(
        scope='offline analysis of existing host/GPU-drain report; no new board execution',
        renderer=report.get('gl_renderer'), frames=len(rows), warmup_frames=warmup,
        target_fps=target_fps, frame_budget_ms=budget_ms,
        gpu_completion=dict(frames_between_certified_endpoints=completed_frames,
                            seconds_between_certified_endpoints=completion_seconds,
                            fps=completed_fps,
                            average_target_met=completed_fps >= target_fps if completed_fps is not None else None,
                            drain_interval_ms=_statistics([g['interval_ms'] for g in groups]),
                            amortized_group_ms_per_frame=_statistics([g['amortized_ms_per_frame'] for g in groups]),
                            groups_over_amortized_budget=sum(g['amortized_ms_per_frame'] > budget_ms for g in groups),
                            maximum_frames_between_drains=max((g['frames'] for g in groups), default=None)),
        drain_anchored_rolling_windows=_one_second_completion_bounds(drains,target_fps,saved),
        chapter_completion=_chapter_completion(rows,drains,saved,warmup,target_fps),
        host=dict(work_ms=_statistics([row['total_ns']/1e6 for row in warm]),
                  finish_interval_ms=_statistics([row['interval_ms'] for row in intervals]),
                  interframe_gap_ms=_statistics([row['interframe_gap_ms'] for row in intervals]),
                  finish_intervals_over_budget=sum(row['interval_ms'] > budget_ms for row in intervals),
                  stage_ms=stage_statistics),
        scoring=dict(parent_score_ns_is_dispatch=bool(report.get('score_process',False)),
                     score_event_timing_key=score_timing_key,
                     child_work_ms=_statistics([row['score_worker_ns']/1e6 for row in warm if 'score_worker_ns' in row]) if report.get('score_process') else None,
                     score_change_count=sum(n in score_change_frames for n, _ in score_frames),
                     score_change_work_ms=_statistics([value for n, value in score_frames if n in score_change_frames]),
                     other_score_work_ms=_statistics([value for n, value in score_frames if n not in score_change_frames]),
                     score_frames_over_20ms=len(heavy_score_frames),
                     over_20ms_coincident_with_score_change=sum(n in score_change_frames for n in heavy_score_frames)),
        diagnostics=dict(saved_frame_count=len(saved),
                         warm_followup_interframe_gap_ms=_statistics(diagnostic_gaps),
                         largest_host_intervals=sorted(intervals, key=lambda row: row['interval_ms'], reverse=True)[:8]),
        boundaries=dict(record_video=bool(report.get('record_video', False)),
                        physical_scanout_measured=False, individual_gpu_completion_measured=False,
                        host_stage_values_are_overlapped_wall_intervals=True,
                        diagnostic_intervals_retained=True,
                        cold_first_frame_ms=rows[0]['total_ns']/1e6),
        notes=[
            'Grouped completion FPS certifies cumulative GPU work, including final drain, not uniform per-frame completion or HDMI scanout.',
            'Host finish intervals include pacing, CPU work, queued GPU work and optional readback/swap; they are not physical display timestamps.',
            'Stage measurements are host wall intervals; JPEG decoding and GPU execution overlap other work and must not be added as independent hardware costs.',
            'Diagnostic snapshots can add readback to their own frame and PNG encoding between that finish timestamp and the following frame start.',
            'Offline MJPG export reports additionally include encoder work between frame timestamps; file FPS is not generation throughput.',
        ])

    if report.get('gpu_completion_events') is not None:
        first=report['gpu_completion_events'][0]
        result['boundaries'].update(
            cold_first_completed_frame_ms=(first['finished_ns']-rows[0]['started_ns'])/1e6,
            host_loop_end_is_same_frame_gpu_completion=False,
            completion_certificate_source='glFinish then matching swap, before next frame GPU commands',
            maximum_gpu_frames_in_flight=1)
        result['notes'].append('Deferred mode host loops contain current preparation/submission and prior frame completion/presentation; use the independent completed-frame certificates, not loop ends, for throughput.')
    return result
