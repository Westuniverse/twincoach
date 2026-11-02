# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in sealed confirmation metadata; old mutable trace stays unchanged."""
import marshal
from posepriv_coach.presentation_trace import (
    DEFAULT_CAPACITY, PresentationTrace, make_presentation_trace as make_mutable_trace,
)


class CompactPresentationTrace(PresentationTrace):
    """Append only after this confirmation row's annotations are complete.

    Bytes are untracked by cyclic GC. Iteration restores the unchanged builtin
    metadata tree after timed rendering; no images or work objects are stored.
    """
    __slots__ = ()

    def append(self, sample):
        self._samples.append(marshal.dumps(sample, 4))
        self._appended += 1

    def __iter__(self):
        return (marshal.loads(row) for row in self._samples)

    def __getitem__(self, index):
        return marshal.loads(self._samples[index])


def make_presentation_trace(cfg):
    if cfg.get('benchmark_presentations', False) is not True:
        return make_mutable_trace(cfg)
    compact = cfg.get('benchmark_presentation_compact', False)
    if type(compact) is not bool:
        raise ValueError('benchmark_presentation_compact must be boolean')
    if not compact:
        return make_mutable_trace(cfg)
    return CompactPresentationTrace(cfg.get('benchmark_presentation_capacity', DEFAULT_CAPACITY))
