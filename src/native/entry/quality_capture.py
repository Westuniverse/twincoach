# SPDX-License-Identifier: GPL-2.0-only
"""Quality-only streaming prepare records. No images, leases or rows are retained."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
import atexit
import json
import math
import os
from pathlib import Path


def plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if hasattr(value, 'tolist'):
        return plain(value.tolist())
    if hasattr(value, 'item'):
        return plain(value.item())
    if hasattr(value, 'value'):
        return plain(value.value)
    raise TypeError('unsupported quality value: '+type(value).__name__)


def pose_record(pose):
    return dict(frame_id=pose.frame_id, rebased_timestamp_ns=pose.timestamp_ns,
                width=pose.source_width, height=pose.source_height,
                points={name: [p.x, p.y, p.confidence]
                        for name, p in pose.keypoints.items()})


def world_record(observation):
    s = observation.source
    return dict(source=[s.view, s.frame_id, s.timestamp_ns, s.width,
                        s.height, s.rotation_degrees],
                world33=[dict(p) for p in observation.world_landmarks],
                normalized33=[dict(p) for p in observation.normalized_landmarks])


class PrepareCapture:
    def __init__(self, owner, session_path, output_path=None):
        self.owner = owner
        self.session_path = Path(session_path)
        self.output_path = None if output_path is None else Path(output_path)
        self.session = None
        self.writer = None
        self.rows = self.empty_rows = self.prepare_errors = 0
        self.closed = False
        atexit.register(self.close)

    def start(self):
        if self.session is not None:
            return
        # First prepare happens only after the feeder wrote the fixed session.
        self.session = json.loads(self.session_path.read_text())
        if self.output_path is None:
            self.output_path = Path(self.session['output_jsonl'])
        self.windows = []
        if self.session.get('window_manifest'):
            frozen = json.loads(Path(self.session['window_manifest']).read_text())
            self.windows = [w for w in frozen['windows']
                            if w['capture_id'] == self.session['capture_id']]
        self.head_slot = tuple(self.owner.asset.bone_names).index('head')
        self.head_bind = self.owner.world.rig.bind[self.head_slot]
        self.writer = self.output_path.open('x', buffering=1)
        self.write(dict(kind='header', schema='posepriv.private.full33_prepare_quality.v1',
                        session=self.session, window_ids=[w['id'] for w in self.windows],
                        bone_names=list(self.owner.asset.bone_names),
                        selection='all_current_take_prepare_results_including_prelude_empty_neutral_held',
                        scope='quality-only record overhead; not performance qualification'))

    def write(self, record):
        self.writer.write(json.dumps(record, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False)+'\n')

    def sources(self, work):
        result = {}
        for view, p in work['poses'].items():
            result[view] = dict(frame_id=p.frame_id,
                               rebased_timestamp_ns=p.timestamp_ns,
                               original_timestamp_ns=p.timestamp_ns-self.session['replay_start_ns']+
                                   self.session['original_min_ns'],
                               width=p.source_width, height=p.source_height)
        return result

    def matching_windows(self, sources):
        selected = []
        for w in self.windows:
            if w.get('human_front_half_open_ns'):
                lo, hi = w['human_front_half_open_ns']
                inside = lo <= sources['front']['original_timestamp_ns'] < hi
            elif w['sample_kind'] == 'original_CFR_samples':
                inside = all(b['first_frame_id'] <= sources[v]['frame_id'] <= b['last_frame_id']
                             and b['first_ns'] <= sources[v]['original_timestamp_ns'] <= b['last_ns']
                             for v, b in w['physical_bounds'].items())
            else:
                lo, hi = w['source_ready_inclusive_ns']
                inside = lo <= max(s['original_timestamp_ns'] for s in sources.values()) <= hi
            if inside:
                selected.append(w['id'])
        return selected

    def input_record(self, work):
        source = self.sources(work)
        raw = {v: pose_record(p) for v, p in work['poses'].items()}
        empty = [v for v, p in work['poses'].items() if not p.keypoints]
        return dict(capture_id=self.session['capture_id'], physical_sources=source,
                    window_ids=self.matching_windows(source), raw17=raw,
                    world33={v: world_record(o) for v, o in work.get('world_observations', {}).items()},
                    empty_views=empty, empty_observation=len(empty) == len(raw),
                    scoring_profile=work.get('scoring_profile'),
                    stream_sources=work.get('stream_sources'))

    def append(self, work, prepared):
        import numpy as np
        self.start()
        record = self.input_record(work)
        plan, rig = prepared['plan'], prepared['rig']
        D = np.asarray(rig.display_transform).reshape(4, 4)
        joints = {}
        for name, joint in plan.joints.items():
            point = np.asarray((*joint.position, 1.))
            joints[name] = dict(position=list(joint.position),
                                display_position=(D@point)[:3].tolist(),
                                confidence=joint.confidence, valid=joint.valid)
        palette = [list(w.matrix.values) for w in plan.matrix_writes]
        skin = np.eye(4)
        skin[:3] = np.asarray(palette[self.head_slot]).reshape(3, 4)/16384
        head_world = D@skin@self.head_bind
        record.update(kind='prepare', prepare_index=self.rows,
                      display_ns=max(p.timestamp_ns for p in work['poses'].values()),
                      display_original_ns=max(s['original_timestamp_ns']
                                              for s in record['physical_sources'].values()),
                      display17={v: pose_record(p) for v, p in
                                 prepared.get('display_poses', work['poses']).items()},
                      reason=rig.reason, head_mode=rig.head_mode,
                      inferred_slots=list(rig.inferred_slots),
                      observed_valid=list(plan.matrix_valid),
                      observed_valid_semantics='Full33 returned plan validity; inferred slots remain inferred',
                      render_valid=list(plan.matrix_valid), joints=joints,
                      observed_joints={n: dict(valid=j.valid, confidence=j.confidence)
                                       for n, j in plan.joints.items()},
                      head_axes_display=head_world[:3, :3].T.tolist(),
                      head_origin_display=head_world[:3, 3].tolist(),
                      matrix_q1_14=palette, display_transform=D.tolist(),
                      observed_core=prepared.get('observed_core'),
                      repetition_count=prepared.get('repetition_count'),
                      completed_score=plain(prepared.get('completed_score')))
        self.write(record)
        self.rows += 1
        self.empty_rows += record['empty_observation']

    def error(self, work, exc):
        self.start()
        record = self.input_record(work)
        record.update(kind='prepare_error', prepare_index=self.rows,
                      exception=type(exc).__name__, message=str(exc))
        self.write(record)
        self.rows += 1
        self.prepare_errors += 1
        self.empty_rows += record['empty_observation']

    def close(self):
        if self.writer is not None and not self.closed:
            self.write(dict(kind='footer', rows=self.rows, empty_rows=self.empty_rows,
                            prepare_errors=self.prepare_errors, closed=True))
            self.writer.close()
        self.closed = True


def install_prepare_capture(owner, session_path=None, output_path=None):
    """Wrap one geometry owner; its original prepare result is returned unchanged."""
    recorder = PrepareCapture(owner, session_path or os.environ['POSEPRIV_QUALITY_SESSION'],
                              output_path or os.environ.get('POSEPRIV_QUALITY_JSONL'))
    original = owner.prepare

    def prepare(work):
        recorder.start()
        try:
            result = original(work)
        except BaseException as exc:
            recorder.error(work, exc)
            raise
        recorder.append(work, result)
        return result

    owner.prepare = prepare
    owner.quality_capture = recorder
    return owner
