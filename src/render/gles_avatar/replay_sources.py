"""Timestamp-preserving inputs for the explicit native board experiment.

No graphics or DMA imports: the same chapter schedule can be checked offline.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass,field
import json
import math
from pathlib import Path

from assemble_hdmi_playlist import causal_frame_indices
from posepriv_coach.contracts import SyncedPosePair
from posepriv_coach.indexed_mjpeg import MjpegIndexRow, load_mjpeg_index
from posepriv_coach.pose_trace import read_pose_trace
from posepriv_coach.sync import DualViewSynchronizer


@dataclass(frozen=True)
class ReplaySample:
    timestamp_ns: int
    rows: dict[str, MjpegIndexRow]
    raw_index: int | None
    pair: SyncedPosePair | None
    display_timestamp_ns: int

    @property
    def observation_ready_ns(self):
        return (max(self.pair.front.timestamp_ns,self.pair.side.timestamp_ns)
                if self.pair is not None else self.timestamp_ns)


@dataclass(frozen=True)
class ReplayChapter:
    title: str
    capture_dir: Path
    raw_pairs: tuple[SyncedPosePair, ...]
    samples: tuple[ReplaySample, ...]
    views: tuple[str, ...]
    rotation_degrees: dict[str, int]

    @property
    def kind(self):
        return 'pose_trace' if self.raw_pairs else 'raw_indexed'


@dataclass(frozen=True)
class ImageDisplaySample:
    """Image-only display source; deliberately has no pose pair/score index."""
    rows: dict[str, MjpegIndexRow]
    display_timestamp_ns: int
    display_keypoints: dict = field(default_factory=dict)


def latest_image_samples(chapter):
    """Use the latest causal raw image on the EXISTING chapter display clock.

    This is a separate display schedule, not a replacement scoring chapter.
    A late camera stays absent until its first capture; real recording gaps
    remain visible as increasing source ages.
    """
    indexes = _indexes(chapter.capture_dir, chapter.views)
    times = {v: tuple(r.timestamp_ns for r in rows) for v, rows in indexes.items()}
    result = []
    for sample in chapter.samples:
        rows = {}
        for view in chapter.views:
            index = bisect_right(times[view], sample.display_timestamp_ns) - 1
            if index >= 0:
                rows[view] = indexes[view][index]
        result.append(ImageDisplaySample(rows, sample.display_timestamp_ns))
    return tuple(result)


def _indexes(capture, views):
    result = {}
    for view in views:
        video = capture / f'{view}.mjpeg'
        result[view] = load_mjpeg_index(capture / f'{view}.timestamps.jsonl',
                                      video_size=video.stat().st_size)
    return result


def _rotations(capture, views, overrides):
    # A recorded rotation is the source's declared presentation transform, not
    # proof of world-up. An explicit reviewed override can correct raw captures.
    path=Path(capture)/'capture_manifest.json'
    metadata=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if overrides is not None and (not isinstance(overrides,dict) or any(v not in views for v in overrides)):
        raise ValueError('rotation overrides must name chapter views')
    result={}
    for view in views:
        angle=(overrides or {}).get(view,metadata.get(f'{view}_rotation_degrees',270 if view=='front' else 90))
        if type(angle) is not int or angle not in (0,90,180,270):
            raise ValueError('rotation must be an integer 0, 90, 180 or 270')
        result[view]=angle
    return result


def pose_chapter(trace, capture, *, title, fps=None, frames=None, rotation_degrees=None):
    sync = DualViewSynchronizer()
    pairs = []
    if frames is not None and (type(frames) is not int or frames <= 0):
        raise ValueError('positive frame limit required')
    for pose in read_pose_trace(trace):
        pair = sync.push(pose)
        if pair is not None:
            pairs.append(pair)
        if frames is not None and len(pairs) >= frames:
            break
    if not pairs or (frames is not None and len(pairs) != frames):
        raise ValueError(f'trace provides {len(pairs)} pairs, requested {frames}')
    capture = Path(capture)
    indexes = {v: {r.frame_id: r for r in rows} for v, rows in
               _indexes(capture, ('front', 'side')).items()}
    # Validate the entire scoring sequence, not just displayed CFR samples.
    for pair in pairs:
        for view in ('front', 'side'):
            pose = getattr(pair, view)
            if indexes[view][pose.frame_id].timestamp_ns != pose.timestamp_ns:
                raise ValueError('trace/index timestamp mismatch')
    ready_times = [max(p.front.timestamp_ns,p.side.timestamp_ns) for p in pairs]
    schedule = (causal_frame_indices(ready_times, fps)
                if fps is not None else range(len(pairs)))
    samples = tuple(ReplaySample(pairs[i].front.timestamp_ns,
        {v: indexes[v][getattr(pairs[i], v).frame_id] for v in indexes}, i, pairs[i],
        ready_times[0]+round(n*1e9/fps) if fps is not None else ready_times[i])
        for n,i in enumerate(schedule))
    views=('front','side')
    return ReplayChapter(title, capture, tuple(pairs), samples, views,_rotations(capture,views,rotation_degrees))


def raw_chapter(capture, *, title, views, fps, rotation_degrees=None):
    views = tuple(views)
    if not views or len(set(views)) != len(views) or any(v not in ('front', 'side') for v in views):
        raise ValueError('unique front/side views required')
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError('positive finite fps required')
    capture = Path(capture)
    indexes = _indexes(capture, views)
    times = {v: [r.timestamp_ns for r in rows] for v, rows in indexes.items()}
    first = min(t[0] for t in times.values())
    last = max(t[-1] for t in times.values())
    samples = []
    for n in range(math.ceil((last-first)*fps/1e9)+1):
        target = first + round(n*1e9/fps)
        rows = {}
        for view in views:
            index = bisect_right(times[view], target)-1
            if index >= 0:
                rows[view] = indexes[view][index]
        samples.append(ReplaySample(target, rows, None, None, target))
    return ReplayChapter(title, capture, (), tuple(samples), views,_rotations(capture,views,rotation_degrees))


def load_playlist(path, *, fps):
    path = Path(path).resolve(strict=True)
    entries = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(entries, list) or not entries:
        raise ValueError('nonempty playlist required')
    chapters = []
    def resolve(value):
        result = Path(value)
        return result if result.is_absolute() else path.parent / result
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('title'), str) or not entry['title'].strip():
            raise ValueError('chapter title required')
        kind = entry['kind']
        capture = resolve(entry['capture_dir'])
        if kind == 'pose_trace':
            chapter = pose_chapter(resolve(entry['trace']), capture, title=entry['title'], fps=fps,
                                   rotation_degrees=entry.get('rotation_degrees'))
        elif kind == 'raw_indexed':
            chapter = raw_chapter(capture, title=entry['title'], views=entry['views'], fps=fps,
                                  rotation_degrees=entry.get('rotation_degrees'))
        else:
            raise ValueError('unknown native playlist kind')
        chapters.append(chapter)
    return tuple(chapters)


def chapter_report(chapter):
    return dict(title=chapter.title, kind=chapter.kind, raw_pairs=len(chapter.raw_pairs),
                frames=len(chapter.samples), views=chapter.views,
                rotation_degrees=chapter.rotation_degrees,
                source_seconds=(max(r.timestamp_ns for r in chapter.samples[-1].rows.values())-
                                min(r.timestamp_ns for r in chapter.samples[0].rows.values()))/1e9,
                first_frame_ids={v: r.frame_id for v, r in chapter.samples[0].rows.items()},
                last_frame_ids={v: r.frame_id for v, r in chapter.samples[-1].rows.items()})


class ReplayScoring:
    """Score original observations once; never carry a session into another chapter."""

    def __init__(self, config):
        self.config = config
        self.chapter_id = None
        self._chapter = None
        self.pipeline = self.display = self.clock = self.state = None
        self.consumed_raw = -1

    def update(self, chapter_id, chapter, sample):
        if self._chapter is not None and chapter_id == self.chapter_id:
            if chapter is not self._chapter:
                raise ValueError('chapter id cannot change its source chapter')
            if sample.raw_index is not None and sample.raw_index < self.consumed_raw:
                raise ValueError('non-monotonic raw score index')
        if chapter_id != self.chapter_id or self._chapter is None:
            self.chapter_id = chapter_id
            self._chapter = chapter
            self.pipeline = self.display = self.clock = self.state = None
            self.consumed_raw = -1
            if chapter.raw_pairs:
                from posepriv_coach import (load_squat_session_processor, DualViewCoachPipeline,
                    PoseFramePassthroughEstimator, CoachDisplayModel)
                from posepriv_coach.replay_clock import ReplayClock
                self.clock = ReplayClock()
                self.pipeline = DualViewCoachPipeline(PoseFramePassthroughEstimator(),
                    load_squat_session_processor(self.config), clock_ns=self.clock)
                self.display = CoachDisplayModel()
        if sample.pair is None:
            return None
        from posepriv_coach import CapturedFrame
        for pair in chapter.raw_pairs[self.consumed_raw+1:sample.raw_index+1]:
            for pose in (pair.front, pair.side):
                self.clock.begin(pose.timestamp_ns)
                self.pipeline.submit(CapturedFrame(pose.view, pose.frame_id, pose.timestamp_ns,
                    pose.source_width, pose.source_height, 'pose_trace', pose))
                step = self.pipeline.process_next()
                self.state = self.display.update(step, self.pipeline.stats())
        self.consumed_raw = sample.raw_index
        return self.state
