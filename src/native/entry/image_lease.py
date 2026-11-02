# SPDX-License-Identifier: GPL-2.0-only
"""Private five-slot decoded arena and explicit, transferable read ownership."""
from contextlib import contextmanager
import queue
from threading import Lock
import time

from posepriv_coach.field_preview_images import (
    SharedImageArena, FREE, READY, READING, LOCK_TIMEOUT,
)
from posepriv_coach.field_preview_process import unpack_inference
from posepriv_coach.mjpeg_recording import MAX_JPEG

LEASE_KEY = '_decoded_image_lease'


class LeasedDecodedArena(SharedImageArena):
    def __init__(self, context, capacity=MAX_JPEG, *, slots=5):
        if slots < 5:
            raise ValueError('three downstream owners require five decoded slots')
        self.capacity = capacity
        self.buffers = tuple(context.RawArray('B', capacity) for _ in range(slots))
        self.states = context.RawArray('i', slots)
        self.identities = context.RawArray('Q', slots*3)
        self.lock = context.Lock()
        self.available = context.Event()
        self._reading = None  # Legacy single take()/borrowed() diagnostics only.

    def acquire(self, timeout=None):
        deadline = None if timeout is None else time.monotonic()+timeout
        while True:
            remaining = LOCK_TIMEOUT if deadline is None else max(0, deadline-time.monotonic())
            if not self.lock.acquire(timeout=remaining):
                raise queue.Empty
            try:
                index = next((i for i,s in enumerate(self.states) if s == READY), None)
                if index is not None:
                    self.states[index] = READING
                    generation, timestamp, length = self.identities[index*3:index*3+3]
            finally:
                self.lock.release()
            if index is not None:
                packet = memoryview(self.buffers[index]).cast('B')[:length]
                return ImageLeaseToken(self, index, generation, timestamp, packet)
            remaining = None if deadline is None else max(0, deadline-time.monotonic())
            if not self.available.wait(remaining):
                raise queue.Empty
            self.available.clear()

    def release_slot(self, index, generation):
        with self._locked():
            if self.states[index] != READING or self.identities[index*3] != generation:
                raise RuntimeError('decoded image lease slot/generation no longer owns READING')
            self.states[index] = FREE

    def reader_count(self):
        with self._locked():
            return sum(s == READING for s in self.states)

    @contextmanager
    def borrowed(self, timeout=None):
        # Preserve the old API for explicit copy-mode callers.
        token = self.acquire(timeout)
        self._reading = token.index
        try:
            yield token.generation, token.timestamp_ns, token.packet
        finally:
            try:token.close('legacy-borrowed-exit')
            finally:self._reading = None


class ImageLeaseToken:
    def __init__(self, arena, index, generation, timestamp_ns, packet):
        self.arena = arena
        self.index = index
        self.generation = generation
        self.timestamp_ns = timestamp_ns
        self.packet = packet
        self.closed = False
        self.reason = None
        self.release_count = 0
        self.release_error = None
        self._lock = Lock()
        self._works = []
        self._on_release = None

    def bind(self, work):
        with self._lock:
            if self.closed:
                raise RuntimeError('cannot expose images after decoded lease release')
            work[LEASE_KEY] = self
            if all(work is not previous for previous in self._works):
                self._works.append(work)
        return work

    def close(self, reason='released'):
        callback = None
        with self._lock:
            if self.closed:return False
            self.closed = True
            self.reason = reason
            self.release_count += 1
            # No completed work may continue exposing an expired shared image.
            for work in self._works:
                work.get('images', {}).clear()
                work.pop(LEASE_KEY, None)
            self._works.clear()
            self.packet.release()
            self.packet = None
            try:
                self.arena.release_slot(self.index, self.generation)
            except BaseException as error:
                # Preserve a failed-lock/generation diagnostic; do not silently
                # reuse this arena after an abnormal producer failure.
                self.release_error = error
                raise
            finally:
                callback = self._on_release
        if callback is not None:callback(self)
        return True


def frame_work(item):
    return item['work'] if item is not None and 'work' in item else item


def token_for(item):
    work = frame_work(item)
    return None if work is None else work.get(LEASE_KEY)


def release_work(item, reason='discarded'):
    token = token_for(item)
    return False if token is None else token.close(reason)


class LeasedInferenceMixin:
    """Use with a supervised InferenceProcess; transferred loans are independent."""
    def _init_image_leases(self):
        self._active_image_leases = set()
        self._image_lease_lock = Lock()

    def _remove_image_lease(self, token):
        with self._image_lease_lock:self._active_image_leases.discard(token)

    def active_image_leases(self):
        with self._image_lease_lock:return len(self._active_image_leases)

    def take_leased(self, timeout=.05):
        # Same supervisor checks as the existing take(), without its implicit
        # self.release(). A previous frame may belong to another stage now.
        if self.process.exitcode is not None:
            self._read_final(.1)
            if self.process.exitcode != 0 or self._final is None:
                raise RuntimeError(f'Inference owner exited {self.process.exitcode} without successful completion')
        elif self.done.is_set():self._read_final(.1)
        try:token = self.output.acquire(timeout)
        except queue.Empty:
            if not self.process.is_alive():
                self._read_final(.1)
                if not self.stop.is_set():
                    raise RuntimeError(f'Inference owner exited {self.process.exitcode}')
            return None
        with self._image_lease_lock:self._active_image_leases.add(token)
        token._on_release = self._remove_image_lease
        try:
            work = unpack_inference(token.packet, zero_copy=True)
            work = self._reconstruct_leased_work(work)
            token.bind(work)
            if self.done.is_set():self._read_final(.1)
            return work
        except BaseException:
            token.close('take-unpack-error')
            raise

    def _reconstruct_leased_work(self, work):
        return work

    def close_image_leases(self):
        # Caller has already cancelled/joined all downstream consumers.
        with self._image_lease_lock:pending = tuple(self._active_image_leases)
        first_error = None
        for token in pending:
            try:token.close('inference-close')
            except BaseException as error:first_error = first_error or error
        if first_error is not None:raise first_error

    def close(self):
        try:self.close_image_leases()
        finally:super().close()
