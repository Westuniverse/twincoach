"""Bounded, aggregate-only knee visibility diagnostics; never alter poses."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from .contracts import CameraView, PoseFrame, SyncedPosePair
from .exercises.squat import SQUAT_KNEE_JOINT_SPECS


@dataclass(slots=True)
class _ChainCounts:
    names: tuple[str, str, str]
    candidates: int = 0
    missing: int = 0
    low_confidence: int = 0
    confidence_qualified: int = 0
    minimum_confidence_sum: float = 0.0
    maximum_minimum_confidence: float | None = None
    missing_by_point: dict[str, int] = field(default_factory=dict)
    low_confidence_by_point: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.missing_by_point = dict.fromkeys(self.names, 0)
        self.low_confidence_by_point = dict.fromkeys(self.names, 0)

    def observe(self, frame: PoseFrame, threshold: float) -> None:
        self.candidates += 1
        points = [frame.keypoints.get(name) for name in self.names]
        for name, point in zip(self.names, points, strict=True):
            if point is None:
                self.missing_by_point[name] += 1
            elif point.confidence < threshold:
                self.low_confidence_by_point[name] += 1
        if any(point is None for point in points):
            self.missing += 1
            return
        minimum = min(point.confidence for point in points)
        self.minimum_confidence_sum += minimum
        self.maximum_minimum_confidence = (
            minimum if self.maximum_minimum_confidence is None
            else max(self.maximum_minimum_confidence, minimum)
        )
        if minimum < threshold:
            self.low_confidence += 1
        else:
            self.confidence_qualified += 1

    def as_record(self) -> dict[str, object]:
        complete = self.candidates - self.missing
        return {
            "candidates": self.candidates,
            "missing": self.missing,
            "low_confidence": self.low_confidence,
            "confidence_qualified": self.confidence_qualified,
            "complete_chain_mean_minimum_confidence": (
                self.minimum_confidence_sum / complete if complete else None
            ),
            "complete_chain_maximum_minimum_confidence": self.maximum_minimum_confidence,
            "missing_by_point": dict(self.missing_by_point),
            "low_confidence_by_point": dict(self.low_confidence_by_point),
        }


class PoseInputQuality:
    """Observe synchronized inputs, not hardware execution or scoring validity.

    The default matches the current PL knee confidence gate. Qualified counts
    do not prove finite/nondegenerate geometry, DMA submission or completion.
    No coordinates, images, timestamps or unbounded sample history are stored.
    """

    def __init__(self, *, minimum_confidence: float = 0.5) -> None:
        if not math.isfinite(minimum_confidence) or not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be finite and within [0, 1]")
        self.minimum_confidence = minimum_confidence
        self.synchronized_pairs = 0
        self._views = {
            view: {
                spec.vertex.removesuffix("_knee"): _ChainCounts(
                    (spec.point_a, spec.vertex, spec.point_c)
                )
                for spec in SQUAT_KNEE_JOINT_SPECS
            }
            for view in CameraView
        }

    def observe_pair(self, pair: SyncedPosePair) -> None:
        self.synchronized_pairs += 1
        for frame in (pair.front, pair.side):
            for chain in self._views[frame.view].values():
                chain.observe(frame, self.minimum_confidence)

    def as_record(self) -> dict[str, object]:
        return {
            "scope": "synchronized_pose_input_confidence_only_not_DMA_or_geometry_validation",
            "minimum_confidence": self.minimum_confidence,
            "synchronized_pairs": self.synchronized_pairs,
            "views": {
                view.value: {side: chain.as_record() for side, chain in chains.items()}
                for view, chains in self._views.items()
            },
        }
