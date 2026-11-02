"""Optional speech for confirmed live frames; never changes scoring or pose data."""
from __future__ import annotations

from types import SimpleNamespace


class PreviewSpeechFeedback:
    def __init__(self, volume=None, *, factory=None):
        if volume is not None and (type(volume) is not int or not -100 <= volume <= 100):
            raise ValueError('speech volume must be an integer in -100..100')
        self.requested = volume is not None
        self.volume = volume
        self.backend = None
        self.failure = None
        self.closed = False
        if not self.requested: return
        if factory is None:
            from .speech_feedback import CoachSpeechFeedback
            factory = CoachSpeechFeedback
        try:
            self.backend = factory(volume=volume, maximum_pending_age_seconds=5.,client_timeout_seconds=20.)
        except (OSError,RuntimeError) as exc:
            self.failure = str(exc)

    def confirmed(self, *, repetition_count, score, source_timestamp_ns, completed_ns, stopping=False):
        if self.closed or self.backend is None or self.failure is not None: return
        if stopping or not 0 <= completed_ns-source_timestamp_ns <= 1_000_000_000:
            self.suspend()
            return
        message = f'已记录第{repetition_count}次完整深蹲。'
        if score is not None:
            message += (f'规则符合度{score.conformance_percent:.0f}分。' if score.valid else
                        '本次观测不足，暂不评分。')
        try:
            self.backend.update(SimpleNamespace(repetition_count=repetition_count,
                                feedback_zh=message,status_zh='正常'))
        except (OSError,RuntimeError) as exc:
            self.failure = str(exc)

    def suspend(self):
        if self.backend is not None: self.backend.discard_pending()

    def stats(self):
        details = self.backend.stats() if self.backend is not None else {}
        failure = self.failure or details.get('failure')
        return dict(details,requested=self.requested,volume=self.volume,
                    enabled=self.backend is not None and failure is None and not self.closed,
                    failure=failure,closed=self.closed,audibility_verified=False)

    def close(self):
        self.closed = True
        if self.backend is not None:
            try: self.backend.close()
            except (OSError,RuntimeError) as exc: self.failure = str(exc)
