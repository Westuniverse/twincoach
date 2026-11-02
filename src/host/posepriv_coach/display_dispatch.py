"""不会让慢速显示写屏反向阻塞实时管线的最新帧调度器。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic_ns
from typing import Any, Callable

from .contracts import PoseFrame


class DisplayDispatchError(RuntimeError):
    """后台显示线程发生异常。"""


@dataclass(frozen=True, slots=True)
class DisplayDispatchStats:
    submitted_frames: int
    rendered_frames: int
    dropped_frames: int
    failed: bool
    measurement_window_ns: int = 0
    total_content_age_ns: int = 0
    max_content_age_ns: int = 0
    latency_window_size: int = 1024
    content_age_frames: int = 0
    content_age_sample_count: int = 0
    p95_content_age_ns: int | None = None
    invalid_content_age_samples: int = 0
    missing_content_timestamp_frames: int = 0

    @property
    def observed_render_fps(self) -> float | None:
        if self.measurement_window_ns <= 0 or self.rendered_frames <= 0:
            return None
        return self.rendered_frames * 1_000_000_000 / self.measurement_window_ns

    @property
    def mean_content_age_ms(self) -> float | None:
        if self.content_age_frames <= 0:
            return None
        return self.total_content_age_ns / self.content_age_frames / 1_000_000

    @property
    def max_content_age_ms(self) -> float | None:
        if self.content_age_frames <= 0:
            return None
        return self.max_content_age_ns / 1_000_000

    @property
    def p95_content_age_ms(self) -> float | None:
        if self.p95_content_age_ns is None:
            return None
        return self.p95_content_age_ns / 1_000_000


class LatestFrameDisplayDispatcher:
    """把渲染器更新放到单线程，并只保留尚未绘制的最新一帧。

    该类适合 framebuffer/HDMI sink：显示端落后时丢弃旧画布，保持采集和 DPU
    继续消费最新同步对。窗口模式仍建议直接使用原渲染器，避免某些桌面后端要求
    ``imshow`` 在主线程调用。
    """

    def __init__(
        self,
        renderer: Any,
        *,
        join_timeout_s: float = 3.0,
        latency_window_size: int = 1024,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        if renderer is None:
            raise ValueError("display renderer must not be None")
        if join_timeout_s <= 0.0:
            raise ValueError("join_timeout_s must be positive")
        if latency_window_size < 1:
            raise ValueError("latency_window_size must be positive")
        self._renderer = renderer
        self._join_timeout_s = join_timeout_s
        self._latency_window_size = latency_window_size
        self._clock_ns = clock_ns
        self._condition = Condition()
        self._latest: tuple[
            tuple[Any, ...],
            dict[str, Any],
            int | None,
        ] | None = None
        self._thread: Thread | None = None
        self._stopping = False
        self._closed = False
        self._submitted = 0
        self._rendered = 0
        self._dropped = 0
        self._failure: BaseException | None = None
        self._measurement_start_ns: int | None = None
        self._measurement_end_ns: int | None = None
        self._total_content_age_ns = 0
        self._max_content_age_ns = 0
        self._content_age_frames = 0
        self._content_age_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._invalid_content_age_samples = 0
        self._missing_content_timestamp_frames = 0

    @property
    def close_requested(self) -> bool:
        return bool(getattr(self._renderer, "close_requested", False))

    def update_pair(self, *args: Any, **kwargs: Any) -> None:
        submitted_ns = self._clock_ns()
        content_timestamp_ns = _pair_timestamp_ns(args)
        with self._condition:
            if self._closed or self._stopping:
                raise DisplayDispatchError("异步显示调度器已经关闭")
            if self._failure is not None:
                raise DisplayDispatchError("后台显示线程失败") from self._failure
            if (
                self._measurement_start_ns is not None
                and submitted_ns < self._measurement_start_ns
            ):
                raise DisplayDispatchError("display dispatch clock must be monotonic")
            if self._latest is not None:
                self._dropped += 1
            self._latest = (tuple(args), dict(kwargs), content_timestamp_ns)
            self._submitted += 1
            if self._measurement_start_ns is None:
                self._measurement_start_ns = submitted_ns
            if self._thread is None:
                self._thread = Thread(
                    target=self._worker_loop,
                    name="posepriv-display",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=self._join_timeout_s)
            if thread.is_alive():
                raise DisplayDispatchError("后台显示线程在限定时间内没有退出")
        close = getattr(self._renderer, "close", None)
        if callable(close):
            close()
        with self._condition:
            self._closed = True
            failure = self._failure
        if failure is not None:
            raise DisplayDispatchError("后台显示线程失败") from failure

    def stats(self) -> Any:
        """返回底层渲染器的真实合成/写屏统计。"""

        stats = getattr(self._renderer, "stats", None)
        if not callable(stats):
            return None
        return stats()

    def dispatch_stats(self) -> DisplayDispatchStats:
        with self._condition:
            measurement_window_ns = (
                self._measurement_end_ns - self._measurement_start_ns
                if self._measurement_start_ns is not None
                and self._measurement_end_ns is not None
                else 0
            )
            return DisplayDispatchStats(
                submitted_frames=self._submitted,
                rendered_frames=self._rendered,
                dropped_frames=self._dropped,
                failed=self._failure is not None,
                measurement_window_ns=measurement_window_ns,
                total_content_age_ns=self._total_content_age_ns,
                max_content_age_ns=self._max_content_age_ns,
                latency_window_size=self._latency_window_size,
                content_age_frames=self._content_age_frames,
                content_age_sample_count=len(self._content_age_samples_ns),
                p95_content_age_ns=_p95_nearest_rank(self._content_age_samples_ns),
                invalid_content_age_samples=self._invalid_content_age_samples,
                missing_content_timestamp_frames=(
                    self._missing_content_timestamp_frames
                ),
            )

    def raise_if_failed(self) -> None:
        with self._condition:
            failure = self._failure
        if failure is not None:
            raise DisplayDispatchError("后台显示线程失败") from failure

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while self._latest is None and not self._stopping:
                    self._condition.wait()
                if self._latest is None and self._stopping:
                    return
                payload = self._latest
                self._latest = None
            assert payload is not None
            args, kwargs, content_timestamp_ns = payload
            try:
                self._renderer.update_pair(*args, **kwargs)
            except BaseException as exc:  # preserve original exception for the caller
                with self._condition:
                    self._failure = exc
                    self._stopping = True
                    self._latest = None
                    self._condition.notify_all()
                return
            rendered_ns = self._clock_ns()
            with self._condition:
                if (
                    self._measurement_start_ns is not None
                    and rendered_ns < self._measurement_start_ns
                ):
                    self._failure = DisplayDispatchError(
                        "display dispatch clock must be monotonic"
                    )
                    self._stopping = True
                    self._latest = None
                    self._condition.notify_all()
                    return
                self._rendered += 1
                self._measurement_end_ns = rendered_ns
                if content_timestamp_ns is not None:
                    content_age_ns = rendered_ns - content_timestamp_ns
                    if content_age_ns < 0:
                        self._invalid_content_age_samples += 1
                    else:
                        self._content_age_frames += 1
                        self._total_content_age_ns += content_age_ns
                        self._max_content_age_ns = max(
                            self._max_content_age_ns,
                            content_age_ns,
                        )
                        self._content_age_samples_ns.append(content_age_ns)
                else:
                    self._missing_content_timestamp_frames += 1
            # Do not keep the completed previews/avatar snapshot alive while
            # waiting for another frame. Release outside the condition lock;
            # the pending latest payload and sink-owned objects remain intact.
            del payload, args, kwargs


def _pair_timestamp_ns(args: tuple[Any, ...]) -> int | None:
    if len(args) < 2 or not isinstance(args[0], PoseFrame) or not isinstance(
        args[1], PoseFrame
    ):
        return None
    return max(args[0].timestamp_ns, args[1].timestamp_ns)


def _p95_nearest_rank(samples_ns: deque[int]) -> int | None:
    if not samples_ns:
        return None
    ordered = sorted(samples_ns)
    rank = max(1, (95 * len(ordered) + 99) // 100)
    return ordered[rank - 1]


__all__ = [
    "DisplayDispatchError",
    "DisplayDispatchStats",
    "LatestFrameDisplayDispatcher",
]
