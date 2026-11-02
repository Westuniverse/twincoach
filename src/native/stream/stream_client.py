"""Bounded continuous PPT4CW01 client; no per-pair barrier (GPL-2.0-only)."""
from collections import deque
from pathlib import Path
import sys
import threading
from time import monotonic

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'client'))
from combined_client import CombinedWorker, worker_environment
import combined_protocol as wire
from posepriv_coach.movenet_cnn_worker import MoveNetCnnWorkerError


class StreamCombinedWorker:
    """One writer/reader, bounded accepted-but-not-consumed source tickets.

    One actor calls submit/take; native server remains its frozen two-slot,
    one-Runner/one-tailfd process. Missing views simply have no submitted ticket.
    Complete replies are published only after full framing and source validation.
    """
    def __init__(self,command,*,capacity=4,timeout=5.,env=None,allow_mock=False,on_process_started=None):
        if not 1<=capacity<=4:raise ValueError('stream ticket capacity must be 1..4')
        self.capacity=capacity;self.timeout=timeout
        self.transport=CombinedWorker(command,timeout=timeout,env=env,allow_mock=allow_mock,
                                      on_process_started=on_process_started)
        self._cv=threading.Condition();self._failure_lock=threading.Lock()
        self._jobs=deque();self._pending=deque();self._results=deque()
        self._writer=self._reader=None
        self._started=self._input_closed=self._writer_done=self._finished=self._aborted=self._closed=False
        self._error=None
        self._submitted=self._written=self._read=self._delivered=self._max_inflight=0
        self._inflight=0

    def start(self):
        if self._started:return
        if self._closed:raise MoveNetCnnWorkerError('stream is closed')
        self.transport.start();self._started=True
        self._writer=threading.Thread(target=self._write_loop,name='tail4-stream-writer')
        self._reader=threading.Thread(target=self._read_loop,name='tail4-stream-reader')
        self._writer.start();self._reader.start()

    def _failure(self,exception):
        # Only one failure owns process cleanup. Never hold the queue lock over
        # cleanup/I/O; killing this owned child wakes the opposite fd owner.
        with self._failure_lock:
            with self._cv:
                if self._aborted:return
                self._aborted=True;self._input_closed=True;self._jobs.clear()
                self._cv.notify_all()
            error=self.transport._fail(exception)
            with self._cv:
                self._error=error;self._cv.notify_all()

    def submit(self,identity,pixels):
        """Return False on full credit; no write/drop/retry/relabel side effect."""
        if not self._started:self.start()
        with self._cv:
            if self._aborted:raise self._error or MoveNetCnnWorkerError('stream aborting')
            if self._input_closed or self._closed:raise MoveNetCnnWorkerError('stream input is closed')
            if self._inflight==self.capacity:return False
            identity=tuple(identity)
            packet=wire.request(*identity,pixels) # one interface validation/pack
            ticket=(self._submitted,identity,packet)
            self._jobs.append(ticket);self._pending.append(ticket[:2])
            self._submitted+=1;self._inflight+=1
            self._max_inflight=max(self._max_inflight,self._inflight)
            self._cv.notify_all()
            return True

    def _write_loop(self):
        try:
            while True:
                with self._cv:
                    self._cv.wait_for(lambda:self._aborted or self._jobs or self._input_closed)
                    if self._aborted:return
                    if not self._jobs:break
                    ticket=self._jobs.popleft()
                self.transport._write(ticket[2],monotonic()+self.timeout)
                with self._cv:self._written+=1
                ticket=None
            self.transport.process.stdin.close()
            with self._cv:self._writer_done=True;self._cv.notify_all()
        except BaseException as exception:self._failure(exception)

    def _read_loop(self):
        try:
            while True:
                with self._cv:
                    self._cv.wait_for(lambda:self._aborted or self._pending or self._writer_done)
                    if self._aborted:return
                    if not self._pending:break
                    ticket=self._pending[0]
                kind,payload=self.transport._record(monotonic()+self.timeout)
                if kind!=2:raise ValueError('stream expected ordered complete response')
                actual,heads=wire.response(payload)
                if actual!=ticket[1]:raise ValueError('stream source ID/frame/ns/geometry mismatch')
                with self._cv:
                    if self._aborted:return
                    self._pending.popleft();self._read+=1;self.transport.frames+=1
                    self._results.append((actual,heads));self._cv.notify_all()
            # Only the reader handles footer/EOF. Writer is finished and stdin
            # already closed, so the inherited bounded lifecycle is safe here.
            self.transport.close()
            with self._cv:self._finished=True;self._cv.notify_all()
        except BaseException as exception:self._failure(exception)

    def take(self,timeout=0):
        """Return one (identity,heads), or None while empty; eof distinguishes EOF.

        Prior complete validated replies remain available after a later failure.
        The first failed/partial/mismatched record is never published.
        """
        timeout=self.timeout if timeout is None else max(0,timeout)
        with self._cv:
            if not(self._results or self._error or self._finished):
                self._cv.wait_for(lambda:self._results or self._error or self._finished,timeout)
            if self._results:
                result=self._results.popleft();self._inflight-=1;self._delivered+=1
                self._cv.notify_all();return result
            if self._error:raise self._error
            return None

    def finish_eof(self):
        if not self._started:self.start()
        with self._cv:
            if self._aborted:raise self._error or MoveNetCnnWorkerError('stream aborting')
            self._input_closed=True;self._cv.notify_all()

    @property
    def eof(self):
        with self._cv:return self._finished and not self._results

    def drain(self,timeout=None):
        """Close submissions, consume remaining <=capacity replies, footer/exit0."""
        self.finish_eof();deadline=monotonic()+(self.timeout if timeout is None else timeout)
        results=[]
        while not self.eof:
            remaining=deadline-monotonic()
            if remaining<=0:
                self._failure(TimeoutError('stream EOF drain deadline expired'))
                raise self._error
            item=self.take(remaining)
            if item is not None:results.append(item)
        self._join();return results

    def _join(self):
        current=threading.current_thread()
        for thread in (self._writer,self._reader):
            if thread is not None and thread is not current:thread.join(self.timeout+.6)

    def cancel(self):
        if self._closed:return
        self._closed=True
        if self._started and not self._finished:self._failure(MoveNetCnnWorkerError('stream cancelled'))
        elif not self._started:self.transport.close()
        self._join()

    def close(self):
        if self._finished:
            self._closed=True;self._join()
        else:self.cancel()

    def stats(self):
        with self._cv:
            state=dict(capacity=self.capacity,max_inflight=self._max_inflight,inflight=self._inflight,
                submitted=self._submitted,written=self._written,responses=self._read,
                delivered=self._delivered,queued_complete=len(self._results),
                input_closed=self._input_closed,finished=self._finished,aborted=self._aborted,
                eof=self._finished and not self._results,error=str(self._error)if self._error else None,
                writer_threads=1 if self._started else 0,reader_threads=1 if self._started else 0)
        state['transport']=self.transport.stats()
        return state
