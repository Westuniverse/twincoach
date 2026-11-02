"""Three bounded decoded-image buffers: writing, latest ready, and reading."""
from contextlib import contextmanager
import queue
import time
from .mjpeg_recording import MAX_JPEG

HEADER_RESERVE=65536
FREE,WRITING,READY,READING=range(4)
LOCK_TIMEOUT=.05


class ImageWriteLease:
    def __init__(self,arena,index):self.arena=arena;self.index=index;self.images={};self.published=False

    def image(self,view,shape):
        import math,numpy as np
        lane={'front':0,'side':1}[view];length=math.prod(shape)
        plane=(self.arena.capacity-HEADER_RESERVE)//2;offset=HEADER_RESERVE+lane*plane
        if len(shape)!=3 or shape[2]!=3 or length>plane:raise ValueError('Decoded preview image exceeds its plane')
        self.images[view]=dict(kind='array',length=length,offset=offset,shape=tuple(shape),dtype='|u1')
        return np.ndarray(shape,dtype=np.uint8,buffer=self.arena.buffers[self.index],offset=offset)


class SharedImageArena:
    def __init__(self,context,capacity=MAX_JPEG):
        self.capacity=capacity;self.buffers=tuple(context.RawArray('B',capacity)for _ in range(3))
        self.states=context.RawArray('i',3);self.identities=context.RawArray('Q',9)
        self.lock=context.Lock();self.available=context.Event();self._reading=None

    @contextmanager
    def _locked(self,timeout=LOCK_TIMEOUT):
        # A process killed inside a multiprocessing Lock never releases it.
        if not self.lock.acquire(timeout=timeout):raise TimeoutError('Decoded image arena lock unavailable')
        try:yield
        finally:self.lock.release()

    @contextmanager
    def writing(self):
        with self._locked():
            index=next((i for i,s in enumerate(self.states)if s==FREE),None)
            if index is None:raise RuntimeError('No free decoded image buffer')
            self.states[index]=WRITING
        lease=ImageWriteLease(self,index)
        try:yield lease
        finally:
            if not lease.published:
                with self._locked():self.states[index]=FREE

    def publish_lease(self,lease,frame_id,timestamp,metadata):
        if len(metadata)>HEADER_RESERVE:raise ValueError('Decoded image metadata exceeds reserved header')
        extent=max(HEADER_RESERVE,max(d['offset']+d['length']for d in lease.images.values()))
        view=memoryview(self.buffers[lease.index]).cast('B')
        try:view[:len(metadata)]=metadata
        finally:view.release()
        with self._locked():
            lost=0
            for i,state in enumerate(self.states):
                if state==READY:self.states[i]=FREE;lost+=1
            self.identities[lease.index*3:lease.index*3+3]=(frame_id,timestamp,extent)
            self.states[lease.index]=READY;lease.published=True
        self.available.set()
        return lost

    @contextmanager
    def borrowed(self,timeout=None):
        deadline=None if timeout is None else time.monotonic()+timeout
        while True:
            remaining=LOCK_TIMEOUT if deadline is None else max(0,deadline-time.monotonic())
            if not self.lock.acquire(timeout=remaining):raise queue.Empty
            try:
                index=next((i for i,state in enumerate(self.states)if state==READY),None)
                if index is not None:
                    self.states[index]=READING;self._reading=index
                    frame_id,timestamp,length=self.identities[index*3:index*3+3]
            finally:self.lock.release()
            if index is not None:break
            remaining=None if deadline is None else max(0,deadline-time.monotonic())
            if not self.available.wait(remaining):raise queue.Empty
            self.available.clear()
        view=memoryview(self.buffers[index]).cast('B')[:length]
        try:yield frame_id,timestamp,view
        finally:
            view.release()
            try:
                with self._locked():self.states[index]=FREE
            finally:self._reading=None

    def pending_count(self,timeout=LOCK_TIMEOUT):
        with self._locked(timeout):return sum(state==READY for state in self.states)

    def discard_pending(self,timeout=LOCK_TIMEOUT):
        with self._locked(timeout):
            count=0
            for i,state in enumerate(self.states):
                if state==READY:self.states[i]=FREE;count+=1
            return count

    def close(self):self.buffers=()
