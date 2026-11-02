"""Two-pair recorded-image lookahead, with one ordered worker per view."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor


class ImageLookahead:
    """Keep at most two future pairs, plus the pair held by the consumer.

    The caller collects both current futures before calling ``consumed``.
    No stream or last-image cache is accessed concurrently within one view.
    Close this owner before closing the image reader or its streams.
    """

    def __init__(self, prepare, work, chapters):
        self.prepare, self.work, self.chapters = prepare, work, chapters
        self.pools = {v: ThreadPoolExecutor(max_workers=1) for v in ('front', 'side')}
        self.pending = deque()
        self.next_submit = self.next_consume = 0
        self.current = None
        self.closed = False

    def _submit(self):
        chapter_id, _, sample = self.work[self.next_submit]
        chapter = self.chapters[chapter_id]
        self.pending.append(tuple(self.pools[v].submit(self.prepare, v, chapter, sample)
                                  for v in ('front', 'side')))
        self.next_submit += 1

    def begin(self, frame_index):
        if self.closed or self.current is not None or frame_index != self.next_consume:
            raise RuntimeError('image lookahead requires ordered begin/consumed calls')
        if frame_index >= len(self.work):
            raise IndexError('image lookahead exhausted')
        if not self.pending:
            while self.next_submit < min(frame_index + 2, len(self.work)):
                self._submit()
        self.current = self.pending.popleft()
        return self.current

    def consumed(self):
        if self.closed or self.current is None or not all(f.done() for f in self.current):
            raise RuntimeError('collect the current image pair before advancing')
        for future in self.current:
            future.result()  # A failed pair must never be treated as consumed.
        self.current = None
        self.next_consume += 1
        if self.next_submit < len(self.work):
            self._submit()

    def close(self):
        if not self.closed:
            self.closed = True
            for pool in self.pools.values():
                pool.shutdown(wait=True, cancel_futures=True)
