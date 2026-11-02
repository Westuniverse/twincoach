"""用于数字人显示路径的低延迟三维关节时序滤波。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from .skeleton_pose import SkeletonJoint3D, Vector3


@dataclass(frozen=True, slots=True)
class JointFilterConfig:
    """One Euro 风格滤波参数；单位均基于秒和赫兹。"""

    minimum_cutoff_hz: float = 1.5
    motion_beta: float = 0.06
    derivative_cutoff_hz: float = 1.0
    reset_gap_ms: float = 250.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_cutoff_hz) or self.minimum_cutoff_hz <= 0.0:
            raise ValueError("minimum_cutoff_hz must be finite and positive")
        if not math.isfinite(self.motion_beta) or self.motion_beta < 0.0:
            raise ValueError("motion_beta must be finite and non-negative")
        if not math.isfinite(self.derivative_cutoff_hz) or self.derivative_cutoff_hz <= 0.0:
            raise ValueError("derivative_cutoff_hz must be finite and positive")
        if not math.isfinite(self.reset_gap_ms) or self.reset_gap_ms <= 0.0:
            raise ValueError("reset_gap_ms must be finite and positive")


@dataclass(frozen=True, slots=True)
class JointFilterStats:
    frames: int
    valid_samples: int
    filtered_samples: int
    reset_samples: int
    invalid_samples: int
    repeated_timestamp_samples: int
    total_correction: float
    max_correction: float

    @property
    def mean_correction(self) -> float | None:
        if self.filtered_samples == 0:
            return None
        return self.total_correction / self.filtered_samples


@dataclass(slots=True)
class _JointFilterState:
    timestamp_ns: int
    raw_position: Vector3
    filtered_position: Vector3
    filtered_derivative: Vector3


class AdaptiveJointFilter:
    """按关节维护自适应低通状态，静止时抑制抖动、运动时降低拖尾。

    无效关节保持无效且不会被历史位置伪造；超过 ``reset_gap_ms`` 后重新出现
    的关节从当前测量重新起步，避免长时间遮挡后的旧状态拖拽。
    """

    def __init__(self, config: JointFilterConfig | None = None) -> None:
        self.config = config if config is not None else JointFilterConfig()
        self._states: dict[str, _JointFilterState] = {}
        self._frames = 0
        self._valid_samples = 0
        self._filtered_samples = 0
        self._reset_samples = 0
        self._invalid_samples = 0
        self._repeated_timestamp_samples = 0
        self._total_correction = 0.0
        self._max_correction = 0.0

    def reset(self) -> None:
        self._states.clear()
        self._frames = 0
        self._valid_samples = 0
        self._filtered_samples = 0
        self._reset_samples = 0
        self._invalid_samples = 0
        self._repeated_timestamp_samples = 0
        self._total_correction = 0.0
        self._max_correction = 0.0

    def update(
        self,
        joints: Mapping[str, SkeletonJoint3D],
        *,
        timestamp_ns: int,
    ) -> dict[str, SkeletonJoint3D]:
        if timestamp_ns < 0:
            raise ValueError("timestamp_ns must be non-negative")
        self._frames += 1
        return {
            name: self._update_joint(joint, timestamp_ns)
            for name, joint in joints.items()
        }

    def _update_joint(
        self,
        joint: SkeletonJoint3D,
        timestamp_ns: int,
    ) -> SkeletonJoint3D:
        if not joint.valid:
            self._invalid_samples += 1
            return joint
        self._valid_samples += 1
        state = self._states.get(joint.name)
        if state is None:
            return self._start(joint, timestamp_ns)
        if timestamp_ns < state.timestamp_ns:
            raise ValueError(f"non-monotonic timestamp for joint {joint.name!r}")
        if timestamp_ns == state.timestamp_ns:
            self._repeated_timestamp_samples += 1
            return _with_position(joint, state.filtered_position)

        elapsed_ns = timestamp_ns - state.timestamp_ns
        if elapsed_ns > round(self.config.reset_gap_ms * 1_000_000):
            return self._start(joint, timestamp_ns)
        elapsed_s = elapsed_ns / 1_000_000_000
        raw_derivative = _scale(_sub(joint.position, state.raw_position), 1.0 / elapsed_s)
        derivative_alpha = _alpha(self.config.derivative_cutoff_hz, elapsed_s)
        filtered_derivative = _lerp(
            state.filtered_derivative,
            raw_derivative,
            derivative_alpha,
        )
        speed = math.sqrt(sum(value * value for value in filtered_derivative))
        cutoff_hz = self.config.minimum_cutoff_hz + self.config.motion_beta * speed
        position_alpha = _alpha(cutoff_hz, elapsed_s)
        filtered_position = _lerp(
            state.filtered_position,
            joint.position,
            position_alpha,
        )
        self._states[joint.name] = _JointFilterState(
            timestamp_ns,
            joint.position,
            filtered_position,
            filtered_derivative,
        )
        correction = math.sqrt(
            sum(
                (raw - filtered) ** 2
                for raw, filtered in zip(
                    joint.position,
                    filtered_position,
                    strict=True,
                )
            )
        )
        self._filtered_samples += 1
        self._total_correction += correction
        self._max_correction = max(self._max_correction, correction)
        return _with_position(joint, filtered_position)

    def _start(self, joint: SkeletonJoint3D, timestamp_ns: int) -> SkeletonJoint3D:
        self._reset_samples += 1
        self._states[joint.name] = _JointFilterState(
            timestamp_ns,
            joint.position,
            joint.position,
            (0.0, 0.0, 0.0),
        )
        return joint

    def stats(self) -> JointFilterStats:
        return JointFilterStats(
            frames=self._frames,
            valid_samples=self._valid_samples,
            filtered_samples=self._filtered_samples,
            reset_samples=self._reset_samples,
            invalid_samples=self._invalid_samples,
            repeated_timestamp_samples=self._repeated_timestamp_samples,
            total_correction=self._total_correction,
            max_correction=self._max_correction,
        )


def _alpha(cutoff_hz: float, elapsed_s: float) -> float:
    time_constant_s = 1.0 / (2.0 * math.pi * cutoff_hz)
    return 1.0 / (1.0 + time_constant_s / elapsed_s)


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return tuple(left - right for left, right in zip(first, second))  # type: ignore[return-value]


def _scale(vector: Vector3, factor: float) -> Vector3:
    return tuple(value * factor for value in vector)  # type: ignore[return-value]


def _lerp(first: Vector3, second: Vector3, amount: float) -> Vector3:
    return tuple(
        left + (right - left) * amount for left, right in zip(first, second)
    )  # type: ignore[return-value]


def _with_position(joint: SkeletonJoint3D, position: Vector3) -> SkeletonJoint3D:
    return SkeletonJoint3D(
        name=joint.name,
        position=position,
        confidence=joint.confidence,
        ray_gap=joint.ray_gap,
        valid=joint.valid,
    )


__all__ = ["AdaptiveJointFilter", "JointFilterConfig", "JointFilterStats"]
