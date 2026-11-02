"""Drop-in bounded stream API using one inherited memfd tensor ring."""
from collections import deque
import mmap
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'client'),
              str(HERE.parent/'stream')]
from combined_client import CombinedWorker,worker_environment
from stream_client import StreamCombinedWorker as PipeStreamWorker
from posepriv_coach.movenet_cnn_worker import MoveNetCnnWorkerError
import shm_protocol as wire


class _ShmTransport(CombinedWorker):
    def __init__(self,*args,memfd,**kwargs):
        super().__init__(*args,**kwargs);self.memfd=memfd

    def _record(self,deadline):
        kind,length=wire.parse_header(self._read(wire.HEADER.size,deadline))
        return kind,self._read(length,deadline)

    def _start(self,deadline):
        self._stderr=tempfile.TemporaryFile(mode='w+b')
        self.process=subprocess.Popen(self.command+[str(self.memfd)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=self._stderr,bufsize=0,start_new_session=True,
            env=self.env,pass_fds=(self.memfd,))
        self._pgid=self.process.pid
        if self._on_process_started is not None:self._on_process_started(self.process.pid)
        os.set_blocking(self.process.stdin.fileno(),False)
        os.set_blocking(self.process.stdout.fileno(),False)
        kind,payload=self._record(deadline)
        if kind!=3:raise ValueError('shared tensor worker must send metadata first')
        self.metadata=wire.metadata(payload,allow_mock=self.allow_mock)


class StreamCombinedWorker(PipeStreamWorker):
    """Four ring slots, two unchanged native slots; take returns owned bytes.

    submit copies source Q7 to a free shared slot then sends a44B ticket.
    The reader verifies the reply identity/slot/generation without reading tensor
    data. take copies the160370B output once, then returns the slot/credit.
    No memoryview/lease escapes; the decoder safely retains owned immutable bytes.
    """
    def __init__(self,command,*,capacity=4,timeout=5.,env=None,allow_mock=False,on_process_started=None):
        super().__init__(command,capacity=capacity,timeout=timeout,env=env,allow_mock=allow_mock,
                         on_process_started=on_process_started)
        self._memfd=os.memfd_create('posepriv-tail4-tensors',os.MFD_CLOEXEC)
        os.ftruncate(self._memfd,wire.RING_BYTES)
        self._ring=mmap.mmap(self._memfd,wire.RING_BYTES,flags=mmap.MAP_SHARED,
                            prot=mmap.PROT_READ|mmap.PROT_WRITE)
        self._free_slots=deque(range(capacity));self._discarded_on_close=0
        self.transport=_ShmTransport(command,timeout=timeout,env=env,allow_mock=allow_mock,
            on_process_started=on_process_started,memfd=self._memfd)

    def start(self):
        try:super().start()
        except BaseException:
            self._closed=True;self._release_ring()
            raise

    def submit(self,identity,pixels):
        if not self._started:self.start()
        with self._cv:
            if self._aborted:raise self._error or MoveNetCnnWorkerError('stream aborting')
            if self._input_closed or self._closed:raise MoveNetCnnWorkerError('stream input is closed')
            if not self._free_slots:return False
            if len(pixels)!=wire.INPUT_BYTES:raise ValueError('shared tensor input must be196608 S8Q7 bytes')
            identity=tuple(identity);slot=self._free_slots[0];generation=self._submitted+1
            packet=wire.request(identity,slot,generation)
            start=slot*wire.SLOT_STRIDE+wire.INPUT_OFFSET
            self._ring[start:start+wire.INPUT_BYTES]=pixels
            self._free_slots.popleft()
            self._jobs.append((self._submitted,identity,packet))
            self._pending.append((self._submitted,identity,slot,generation))
            self._submitted+=1;self._inflight+=1
            self._max_inflight=max(self._max_inflight,self._inflight)
            self._cv.notify_all();return True

    def _read_loop(self):
        try:
            while True:
                with self._cv:
                    self._cv.wait_for(lambda:self._aborted or self._pending or self._writer_done)
                    if self._aborted:return
                    if not self._pending:break
                    ticket=self._pending[0]
                kind,payload=self.transport._record(monotonic()+self.timeout)
                if kind!=2:raise ValueError('shared tensor worker expected complete ordered ticket')
                identity,slot,generation=wire.response(payload)
                if(identity,slot,generation)!=(ticket[1],ticket[2],ticket[3]):
                    raise ValueError('shared tensor source ID/frame/ns/geometry/slot/generation mismatch')
                with self._cv:
                    if self._aborted:return
                    self._pending.popleft();self._read+=1;self.transport.frames+=1
                    self._results.append((identity,slot,generation));self._cv.notify_all()
            self.transport.close()
            with self._cv:self._finished=True;self._cv.notify_all()
        except BaseException as exception:self._failure(exception)

    def take(self,timeout=0):
        timeout=self.timeout if timeout is None else max(0,timeout)
        with self._cv:
            if not(self._results or self._error or self._finished):
                self._cv.wait_for(lambda:self._results or self._error or self._finished,timeout)
            if self._results:
                identity,slot,_generation=self._results.popleft()
                offset=slot*wire.SLOT_STRIDE+wire.OUTPUT_OFFSET
                heads={}
                for role,size in zip(wire.ROLES,wire.SIZES):
                    heads[role]=self._ring[offset:offset+size];offset+=size
                # Copy completed under the owner lock before this slot can be
                # reused. Raw bytes outlive every future native/source request.
                self._free_slots.append(slot);self._inflight-=1;self._delivered+=1
                self._cv.notify_all();return identity,heads
            if self._error:raise self._error
            return None

    def _release_ring(self):
        # close/cancel end the consumer lifetime. The normal actor calls drain
        # before close; explicit cancellation discards remaining complete tickets.
        with self._cv:
            self._discarded_on_close+=len(self._results);self._results.clear()
            if self._ring is not None:self._ring.close();self._ring=None
            if self._memfd is not None:os.close(self._memfd);self._memfd=None

    def cancel(self):
        try:super().cancel()
        finally:self._release_ring()

    def close(self):
        try:super().close()
        finally:self._release_ring()

    def stats(self):
        value=super().stats()
        value.update(transport_kind='memfd_ring4_short_tickets',ring_bytes=wire.RING_BYTES,
            ring_slot_stride=wire.SLOT_STRIDE,ticket_payload_bytes=wire.TICKET_BYTES,
            free_ring_slots=len(self._free_slots),discarded_complete_on_close=self._discarded_on_close,
            output_ownership='take copies each raw head once, then releases ring slot; no lease escapes')
        return value
