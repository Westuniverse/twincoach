"""One pending cumulative movie report; file IO never runs on the play thread."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Condition, Thread


class LatestMovieReport:
    """Serialize snapshots on submit, atomically write the latest in a worker.

    At most one snapshot is being written and one is pending. Intermediate
    reports can be replaced; media frames and cumulative counters cannot.
    close drains the final submission and propagates file errors. It can wait
    for slow storage; this wait belongs to shutdown, not the playback loop.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._condition = Condition()
        self._pending = None
        self._closed = False
        self._error = None
        self._thread = Thread(target=self._run, name='movie-report')
        self._thread.start()

    def submit(self, snapshot):
        payload = json.dumps(snapshot, indent=2, allow_nan=False) + '\n'
        with self._condition:
            if self._error is not None:
                raise self._error
            if self._closed:
                raise RuntimeError('movie report is closed')
            self._pending = payload
            self._condition.notify()

    def _write(self, payload):
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(payload, encoding='utf-8')
        temporary.replace(self.path)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._closed)
                if self._pending is None:
                    return
                payload, self._pending = self._pending, None
            try:
                self._write(payload)
            except BaseException as exc:
                with self._condition:
                    self._error = exc
                    self._pending = None
                    self._closed = True
                return

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join()
        if self._error is not None:
            raise self._error
