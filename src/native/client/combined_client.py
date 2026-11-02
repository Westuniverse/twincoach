"""Private constant Runner+tailfd client; reuse project bounded fd I/O (GPL-2.0-only)."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import queue
import subprocess
import tempfile
from time import monotonic

from posepriv_coach.movenet_cnn_worker import _WorkerProcess, MoveNetCnnWorkerError
import combined_protocol as wire


def worker_environment(firmware, *, inherited=None):
    """Select current isolation firmware before cold Runner creation."""
    path = Path(firmware)
    if path.name != 'posepriv-dpu-isolation.xclbin' or not path.is_file():
        raise ValueError('pass the readable current posepriv-dpu-isolation.xclbin')
    env = dict(os.environ if inherited is None else inherited)
    env.update(LD_LIBRARY_PATH='/opt/vitis-ai-compat/lib:/lib/aarch64-linux-gnu:/lib',
               XLNX_VART_FIRMWARE=str(path.resolve()), XLNX_DISABLE_LOAD_XCLBIN='0')
    return env


class CombinedWorker(_WorkerProcess):
    """One binary child; pair calls overlap prefix B with PL tail A.

    The client owns response ordering. Raw byte heads are not decoded here.
    ``close``/``finish`` drains the count footer, EOF and successful child exit.
    """

    def __init__(self, command, *, timeout=5.0, env=None, allow_mock=False, on_process_started=None):
        self.command = list(command)
        self.timeout = timeout
        self.env = env
        self.allow_mock = allow_mock
        self._on_process_started = on_process_started
        self.process = self.metadata = self._stderr = self._pgid = None
        self.frames = 0
        self.closed = self.complete = False
        self.error = None
        self._stderr_tail = ''
        self._own_executor = None

    def _record(self, deadline):
        kind, length = wire.parse_header(self._read(wire.HEADER.size, deadline))
        return kind, self._read(length, deadline)

    def _start(self, deadline):
        self._stderr = tempfile.TemporaryFile(mode='w+b')
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr, bufsize=0, start_new_session=True, env=self.env)
        self._pgid = self.process.pid
        if self._on_process_started is not None:
            self._on_process_started(self.process.pid)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        kind, payload = self._record(deadline)
        if kind != 3:
            raise ValueError('combined worker must send metadata first')
        self.metadata = wire.metadata(payload, allow_mock=self.allow_mock)

    def start(self):
        if self.closed:
            raise MoveNetCnnWorkerError(self.error or 'combined worker is closed')
        if self.process is None:
            try:
                self._start(monotonic() + self.timeout)
            except BaseException as exc:
                error = self._fail(exc)
                if not isinstance(exc, Exception):
                    raise
                raise error from exc

    def infer(self, identity, pixels):
        if self.closed:
            raise MoveNetCnnWorkerError(self.error or 'combined worker is closed')
        deadline = monotonic() + self.timeout
        try:
            payload = wire.request(*identity, pixels)
            if self.process is None:
                self._start(deadline)
            self._write(payload, deadline)
            kind, payload = self._record(deadline)
            if kind != 2:
                raise ValueError('combined worker expected one response')
            actual, heads = wire.response(payload)
            if actual != tuple(identity):
                raise ValueError('combined source ID/frame/ns/geometry mismatch')
            self.frames += 1
            return heads
        except BaseException as exc:
            error = self._fail(exc)
            if not isinstance(exc, Exception):
                raise
            raise error from exc

    @contextmanager
    def pipelined(self, identities, pixels, executor=None):
        """One response owner, one bounded writer; consume before context exit."""
        if self.closed:
            raise MoveNetCnnWorkerError(self.error or 'combined worker is closed')
        identities = tuple(tuple(value) for value in identities)
        deadline = monotonic() + self.timeout
        writer = None
        read_count = 0
        writer_failures = queue.Queue(maxsize=1)
        transport_failure = False
        try:
            if executor is None:
                if self._own_executor is None:
                    self._own_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tail4-wire')
                executor = self._own_executor
            if self.process is None:
                self._start(deadline)
            def write_all():
                try:
                    for identity, value in zip(identities, pixels, strict=True):
                        self._write(wire.request(*identity, value), deadline)
                except BaseException as exc:
                    writer_failures.put_nowait(exc)
                    try:
                        self.process.stdin.close()
                    except OSError:
                        pass
                    raise
            writer = executor.submit(write_all)
            def responses():
                nonlocal read_count, transport_failure
                for identity in identities:
                    try:
                        kind, payload = self._record(deadline)
                    except (ValueError, TimeoutError):
                        transport_failure = True
                        raise
                    if kind != 2:
                        transport_failure = True
                        raise ValueError('combined worker expected ordered response')
                    actual, heads = wire.response(payload)
                    if actual != identity:
                        raise ValueError('combined source identity mismatch')
                    self.frames += 1
                    read_count += 1
                    yield heads
            yield responses()
            if read_count != len(identities):
                raise ValueError('combined responses were not fully consumed')
            writer.result(timeout=max(0, deadline - monotonic()))
        except BaseException as exc:
            cause = exc
            if transport_failure and isinstance(exc, Exception):
                try:
                    cause = writer_failures.get_nowait()
                except queue.Empty:
                    pass
            error = self._fail(cause)
            if writer is not None:
                try:
                    writer.result(timeout=.6)
                except BaseException:
                    pass
            if not isinstance(cause, Exception):
                raise cause
            raise error from cause

    def infer_pair(self, identities, pixels, executor=None):
        identities = tuple(identities)
        if len(identities) != 2:
            raise ValueError('combined paired call requires exactly two identities')
        with self.pipelined(identities, pixels, executor) as responses:
            return list(responses)

    def finish(self):
        self.close()

    def close(self):
        try:
            super().close()
        finally:
            if self._own_executor is not None:
                self._own_executor.shutdown(wait=True, cancel_futures=True)
                self._own_executor = None

    def stats(self):
        value = super().stats()
        value['input_name_provenance'] = 'cold actual Runner tensor name/shape/Q'
        value['scope'] = 'persistent first60DPU + 35PL tail per source; raw heads, no CPU NN/float requantization'
        return value
