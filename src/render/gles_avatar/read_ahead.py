"""Bounded Linux file advice; it never chooses or decodes a future frame."""
from dataclasses import dataclass
import os


@dataclass
class _FileAdvice:
    chapter: object
    stream: object
    size: int
    last_offset: int
    advised_until: int


class VideoReadAhead:
    WINDOW_BYTES = 4*1024*1024

    def __init__(self):
        if not hasattr(os,'posix_fadvise'):
            raise RuntimeError('video read-ahead requires POSIX file advice')
        self._views={}
        self.requests=0

    def clear(self,view):
        self._views.pop(view,None)

    def request(self,view,chapter,stream,row):
        if view not in ("front","side"):
            raise ValueError("front or side view required")
        previous=self._views.get(view)
        if (previous is None or previous.chapter is not chapter or previous.stream is not stream
                or row.offset<previous.last_offset):
            previous=_FileAdvice(chapter,stream,os.fstat(stream.fileno()).st_size,row.offset,row.offset)
            self._views[view]=previous
        if (previous.advised_until<previous.size and
                row.offset+row.length+self.WINDOW_BYTES//2>=previous.advised_until):
            start=max(row.offset,previous.advised_until)
            end=min(previous.size,row.offset+self.WINDOW_BYTES)
            if end>start:
                os.posix_fadvise(stream.fileno(),start,end-start,os.POSIX_FADV_WILLNEED)
                previous.advised_until=end
                self.requests+=1
        previous.last_offset=row.offset
