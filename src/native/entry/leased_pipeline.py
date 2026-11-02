# SPDX-License-Identifier: GPL-2.0-only
"""Private single-owner stage transfers and post-upload image release."""
from contextlib import contextmanager
from time import monotonic_ns
from posepriv_coach.field_preview_pipeline import LatestInferenceWorker
from posepriv_coach.field_preview_skinning import execute_prepared_skinning
from image_lease import frame_work, token_for, release_work


def snapshot_leased_work(owner):
    work = owner.take_leased()
    if work is None:return None
    token = token_for(work)
    try:
        started = monotonic_ns()
        result = dict(work, images=dict(work['images']), selected=dict(work['selected']),
            poses=dict(work['poses']), stages={v:dict(s) for v,s in work['stages'].items()})
        result['geometry_image_snapshot_ms'] = (monotonic_ns()-started)/1e6
        result['decoded_image_transport'] = 'explicit_lease_no_snapshot_pixel_copy'
        return token.bind(result)
    except BaseException:
        token.close('snapshot-metadata-error')
        raise


def transform_owned(item, function, stopped, *, maximum_age_ns=None):
    if item is None:return None
    transferred = False
    try:
        work = frame_work(item)
        if stopped.is_set():return None
        if (maximum_age_ns is not None and
                monotonic_ns()-min(r[1] for r in work['selected'].values()) > maximum_age_ns):
            return None
        result = function(item)
        if result is None:return None
        token = token_for(item)
        if token is not None:token.bind(frame_work(result))
        transferred = True
        return result
    finally:
        if not transferred:release_work(item, 'stage-drop-or-error')


class LeasedLatestWorker(LatestInferenceWorker):
    """The unchanged one-READY scheduling policy, with an explicit disposer."""
    def __init__(self, work, stopped, *, name, block_when_ready=True):
        super().__init__(work, stopped, name=name, block_when_ready=block_when_ready)

    def _run(self):
        try:
            while not self._stop.is_set() and not self._external_stop.is_set():
                if self._block_when_ready:
                    with self._changed:
                        while (self._ready is not None and not self._stop.is_set()
                               and not self._external_stop.is_set()):self._changed.wait(.05)
                        if self._stop.is_set() or self._external_stop.is_set():break
                item = self._work()
                if item is None:
                    self._stop.wait(.001)
                    continue
                discard = None
                with self._changed:
                    self._completed += 1
                    if self._stop.is_set() or self._external_stop.is_set():
                        self._discarded += 1
                        discard = item
                    else:
                        if self._ready is not None:
                            self._superseded += 1
                            discard = self._ready
                        self._ready = item
                    self._changed.notify_all()
                release_work(discard, 'worker-discard')
        except BaseException as error:
            with self._changed:
                self._error = error
                self._changed.notify_all()

    def close(self):
        self._stop.set()
        with self._changed:self._changed.notify_all()
        self._thread.join()
        with self._changed:
            pending, self._ready = self._ready, None
            if pending is not None:self._discarded += 1
            self._closed = True
            self._changed.notify_all()
            error = self._error if not self._reported_error else None
            if error is not None:self._reported_error = True
        try:release_work(pending, 'worker-close-ready')
        finally:
            if error is not None:raise error


def make_skinning_worker(geometry_worker, backend, stopped, *, execute=execute_prepared_skinning):
    def next_skinning():
        return transform_owned(geometry_worker.take(),
            lambda prepared:execute(prepared, backend, stopped), stopped)
    return LeasedLatestWorker(next_skinning, stopped,
        name='posepriv-skinning', block_when_ready=True)


class UploadLeaseRelease:
    """Native call return releases images before EGL wait; outer finally covers errors."""
    def __init__(self, painter):
        self.current = None
        native = getattr(painter, 'native_gl_submit', None)
        self.native = native
        self.original_call = None if native is None else native.call
        if native is not None:
            def call(*args):
                try:return self.original_call(*args)
                finally:self.release('native-upload-return')
            native.call = call

    def release(self, reason='frame-finally'):
        current, self.current = self.current, None
        return release_work(current, reason)

    @contextmanager
    def frame(self, item):
        if self.current is not None:
            raise RuntimeError('one image lease must finish before binding the next GPU frame')
        self.current = item
        try:yield
        finally:self.release()

    def close(self):
        try:self.release('upload-owner-close')
        finally:
            if self.native is not None:self.native.call = self.original_call
