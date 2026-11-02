"""One-pending-request score worker for the explicit native replay experiment."""
from __future__ import annotations

import math
from multiprocessing import Pipe
from pathlib import Path
import subprocess
import sys
import time

PROTOCOL_VERSION=1


class ScoreProcessError(RuntimeError):
    pass


class ScoreProcess:
    def __init__(self, playlist, config, *, fps=16., startup_timeout=30., response_timeout=5.):
        if any(not math.isfinite(value) or value<=0 for value in (fps,startup_timeout,response_timeout)):
            raise ValueError('positive finite fps/timeouts required')
        self._connection,child=Pipe(duplex=True)
        self._process=None
        self._pending=None
        self._last_request=None
        self._submitted_ns=None
        self._closed=False
        self.fault=False
        self.response_timeout=response_timeout
        self.last_worker_ns=self.last_roundtrip_ns=self.last_receive_wait_ns=None
        self.last_max_rss_kib=self.last_consumed_raw_pairs=None
        worker=Path(__file__).with_name('score_process_worker.py')
        try:
            self._process=subprocess.Popen([sys.executable,str(worker),'--fd',str(child.fileno()),
                '--playlist',str(Path(playlist).resolve()),'--config',str(Path(config).resolve()),
                '--fps',str(fps)],pass_fds=(child.fileno(),),stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,start_new_session=True)
            child.close()
            message=self._read(startup_timeout)
            if (not isinstance(message,tuple) or len(message)!=2 or message[0]!='ready'
                    or not isinstance(message[1],dict) or message[1].get('protocol_version')!=PROTOCOL_VERSION):
                raise ScoreProcessError(f'score worker initialization failed: {message!r}')
            self.ready=message[1]
        except BaseException:
            child.close();self.close()
            raise

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def pending(self):
        return self._pending is not None

    @property
    def closed(self):
        return self._closed

    def _read(self, timeout):
        try:
            if not self._connection.poll(timeout):
                raise ScoreProcessError('score worker response timeout')
            return self._connection.recv()
        except (EOFError,OSError) as error:
            raise ScoreProcessError(f'score worker EOF/connection failure: {error}') from error

    def submit(self, chapter_id, chapter_slot, local_frame):
        if self._closed or self.fault:
            raise ScoreProcessError('score worker is closed or faulted')
        if self._pending is not None:
            raise RuntimeError('only one score request may be pending')
        request=(chapter_id,chapter_slot,local_frame)
        if any(type(value) is not int or value<0 for value in request):
            raise ValueError('nonnegative integer chapter/frame indices required')
        if self._last_request is not None and chapter_id==self._last_request[0]:
            if chapter_slot!=self._last_request[1]:
                raise ValueError('chapter slot change requires a new chapter_id')
            if local_frame<self._last_request[2]:
                raise ValueError('backward score frame requires a new chapter_id')
        self._submitted_ns=time.perf_counter_ns()
        try:
            self._connection.send(('score',request))
        except (OSError,EOFError) as error:
            self.fault=True;self.close()
            raise ScoreProcessError(f'cannot send score request: {error}') from error
        self._pending=request

    def receive(self):
        if self._closed or self.fault:
            raise ScoreProcessError('score worker is closed or faulted')
        if self._pending is None:
            raise RuntimeError('no score request is pending')
        started=time.perf_counter_ns()
        try:
            message=self._read(self.response_timeout)
            if not isinstance(message,tuple) or len(message)!=5 or message[0]!='result' or message[1]!=self._pending:
                raise ScoreProcessError(f'score worker request failed: {message!r}')
            _,_,state,worker_ns,stats=message
            max_rss_kib=stats['max_rss_kib'];consumed_raw_pairs=stats['consumed_raw_pairs']
        except BaseException:
            self.fault=True;self.close()
            raise
        end=time.perf_counter_ns()
        self.last_worker_ns=worker_ns
        self.last_roundtrip_ns=end-self._submitted_ns
        self.last_receive_wait_ns=end-started
        self.last_max_rss_kib=max_rss_kib
        self.last_consumed_raw_pairs=consumed_raw_pairs
        self._last_request=self._pending
        self._pending=None;self._submitted_ns=None
        return state

    def close(self):
        if self._closed:
            return
        self._closed=True
        if self._process is not None and self._process.poll() is None and self._pending is None:
            try:self._connection.send(('close',None))
            except (OSError,EOFError):pass
        self._connection.close()
        if self._process is not None:
            try:self._process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:self._process.wait(timeout=1.)
                except subprocess.TimeoutExpired:
                    self._process.kill();self._process.wait(timeout=1.)
        self._pending=None

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()
