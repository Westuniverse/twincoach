"""Bounded local Chinese speech; the video/session clock never waits for audio."""
from __future__ import annotations

import shutil
import subprocess
import time
import math


class CoachSpeechFeedback:
    def __init__(self, *, clock=time.monotonic, popen=subprocess.Popen,
                 volume=None, maximum_pending_age_seconds=None,client_timeout_seconds=None):
        if volume is not None and (type(volume) is not int or not -100 <= volume <= 100):
            raise ValueError('Speech Dispatcher volume must be an integer in -100..100')
        if maximum_pending_age_seconds is not None and (
                type(maximum_pending_age_seconds) not in (int,float)
                or not math.isfinite(maximum_pending_age_seconds) or maximum_pending_age_seconds <= 0):
            raise ValueError('Pending speech age must be finite and positive')
        if client_timeout_seconds is not None and (type(client_timeout_seconds) not in (int,float)
                or not math.isfinite(client_timeout_seconds) or client_timeout_seconds<=0):
            raise ValueError('Speech client timeout must be finite and positive')
        self._command = shutil.which("spd-say")
        if self._command is None:
            raise RuntimeError("中文播报需要本地 Speech Dispatcher（spd-say）")
        self._clock, self._popen = clock, popen
        self._volume = volume
        self._maximum_pending_age = maximum_pending_age_seconds
        self._client_timeout = client_timeout_seconds
        self._process = None
        self._pending = "运动教练已就绪。请保持全身在画面中，双脚与肩同宽，缓慢下蹲。"
        self._pending_since = self._clock()
        self._discarded_pending = 0
        self._last_started = float("-inf")
        self._last_count = 0
        self._last_status = None
        self._spoken = 0
        self._failure = None
        self._closed = False

    def update(self, state):
        if self._closed or self._failure is not None:
            return
        if state.repetition_count > self._last_count:
            self._pending = state.feedback_zh or f"已完成{state.repetition_count}次动作。"
            self._pending_since = self._clock()
        self._last_count = state.repetition_count
        if state.status_zh == "摄像头采集异常" and self._last_status != state.status_zh:
            self._pending = "摄像头连接异常，请检查连接和取景。"
            self._pending_since = self._clock()
        self._last_status = state.status_zh
        if self._process is not None:
            result = self._process.poll()
            if result is None:
                if self._client_timeout is not None and self._clock()-self._last_started>=self._client_timeout:
                    self._failure=f'本地语音客户端超过{self._client_timeout:g}秒，已请求停止'
                    self.discard_pending()
                    try:self._process.terminate()
                    except OSError:pass
                return
            self._process = None
            if result != 0:
                self._failure = f"本地语音服务退出码 {result}"
                return
        now = self._clock()
        if (self._pending is not None and self._maximum_pending_age is not None
                and now-self._pending_since > self._maximum_pending_age):
            self.discard_pending()
        if self._pending is None or now - self._last_started < 4.0:
            return
        try:
            command = [self._command, "-o", "espeak-ng", "-l", "cmn", "-r", "-10", "-w"]
            if self._volume is not None: command.extend(("-i", str(self._volume)))
            self._process = self._popen(
                [*command, "--", self._pending],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self._failure = str(error)
            return
        self._pending = None
        self._last_started = now
        self._spoken += 1

    def discard_pending(self):
        if self._pending is not None: self._discarded_pending += 1
        self._pending = None

    def stats(self):
        return {"backend": "local_speech_dispatcher_espeak_ng", "language": "cmn",
                "utterances_started": self._spoken, "failure": self._failure,
                "volume": self._volume, "discarded_pending": self._discarded_pending,
                "maximum_pending_age_seconds": self._maximum_pending_age,
                "client_timeout_seconds": self._client_timeout,
                "audibility_verified": False,
                "pending": self._pending is not None, "closed": self._closed}

    def close(self):
        self._closed = True
        self._pending = None
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = None
