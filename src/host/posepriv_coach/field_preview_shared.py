"""Single producer/consumer latest JPEG, without a multiprocessing pipe copy."""
import queue
import time
from contextlib import contextmanager
from .mjpeg_recording import MAX_JPEG


class SharedJpegSlot:
    def __init__(self, context, capacity=MAX_JPEG):
        self.capacity=capacity;self._storage=context.RawArray('B',capacity)
        self._identity=context.RawArray('Q',4)
        self._lock=context.Lock();self._ready=context.Event()

    def publish(self, record, *, capture_epoch=None):
        frame_id,timestamp,payload=record
        return self.publish_parts(frame_id,timestamp,(payload,),capture_epoch=capture_epoch)

    def publish_parts(self,frame_id,timestamp,parts,*,capture_epoch=None):
        result=self.try_publish_parts(frame_id,timestamp,parts,capture_epoch=capture_epoch)
        return 1 if result is None else result

    def try_publish_parts(self,frame_id,timestamp,parts,*,capture_epoch=None):
        if capture_epoch is not None and (type(capture_epoch)is not int or not 0<=capture_epoch<2**64-1):
            raise ValueError('Invalid explicit capture epoch')
        views=tuple(memoryview(part).cast('B')for part in parts)
        try:
            length=sum(view.nbytes for view in views)
            if (type(frame_id)is not int or not 0<=frame_id<2**64 or
                    type(timestamp)is not int or not 0<timestamp<2**64 or not 0<length<=self.capacity):
                raise ValueError('Invalid shared preview frame identity or size')
            # The raw recorder must never wait for the reader's image copy.
            if not self._lock.acquire(False):return None
            try:
                dropped=int(self._ready.is_set());destination=memoryview(self._storage).cast('B');offset=0
                try:
                    for view in views:
                        destination[offset:offset+view.nbytes]=view;offset+=view.nbytes
                finally:destination.release()
                self._identity[:]=(frame_id,timestamp,length,0 if capture_epoch is None else capture_epoch+1);self._ready.set()
                return dropped
            finally:self._lock.release()
        finally:
            for view in views:view.release()

    @contextmanager
    def borrowed(self, timeout=None):
        """Hold the immutable packet only while the internal decoder consumes it."""
        deadline=None if timeout is None else time.monotonic()+timeout
        if not self._ready.wait(timeout):raise queue.Empty
        remaining=.05 if deadline is None else max(0,deadline-time.monotonic())
        if not self._lock.acquire(timeout=remaining):raise queue.Empty
        try:
            if not self._ready.is_set():raise queue.Empty
            frame_id,timestamp,length,epoch_tag=self._identity
            view=memoryview(self._storage).cast('B')[:length]
            try:
                yield (frame_id,timestamp,view)if epoch_tag==0 else(frame_id,timestamp,view,epoch_tag-1)
            finally:view.release();self._ready.clear()
        finally:self._lock.release()

    def get(self, timeout=None):
        with self.borrowed(timeout)as record:
            frame_id,timestamp,payload=record[:3]
            result=(frame_id,timestamp,bytes(payload))
            return result if len(record)==3 else(*result,record[3])

    def close(self):
        # Caller has already stopped/joined the only consumer process.
        self._storage=None;self._identity=None


class SharedResultChannel:
    """One leased image pair and one latest pending pair, both bounded."""
    def __init__(self,context,capacity=MAX_JPEG):
        self.slots=(SharedJpegSlot(context,capacity),SharedJpegSlot(context,capacity))
        self.available=context.Event();self._last_write=0;self._reading=None

    def publish(self,record):
        frame_id,timestamp,payload=record
        return self.publish_parts(frame_id,timestamp,(payload,))

    def publish_parts(self,frame_id,timestamp,parts):
        for index in (self._last_write,1-self._last_write):
            lost=self.slots[index].try_publish_parts(frame_id,timestamp,parts)
            if lost is not None:
                self._last_write=index;self.available.set();return lost
        return 1

    @contextmanager
    def borrowed(self,timeout=None):
        from time import monotonic
        deadline=None if timeout is None else monotonic()+timeout
        while True:
            for index,slot in enumerate(self.slots):
                if slot._ready.is_set():
                    try:
                        context=slot.borrowed(timeout=0);packet=context.__enter__()
                    except queue.Empty:continue
                    try:
                        self._reading=index
                        try:yield packet
                        finally:self._reading=None
                    finally:context.__exit__(None,None,None)
                    return
            remaining=None if deadline is None else max(0,deadline-monotonic())
            if not self.available.wait(remaining):raise queue.Empty
            self.available.clear()

    def get(self,timeout=None):
        with self.borrowed(timeout)as(frame_id,timestamp,payload):return frame_id,timestamp,bytes(payload)

    def pending_count(self,timeout=.05):
        return sum(int(slot._ready.is_set())for index,slot in enumerate(self.slots)if index!=self._reading)

    def discard_pending(self,timeout=.05):
        count=self.pending_count()
        for slot in self.slots:slot._ready.clear()
        return count

    def close(self):
        for slot in self.slots:slot.close()
