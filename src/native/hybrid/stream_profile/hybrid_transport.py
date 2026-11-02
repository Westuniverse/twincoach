# SPDX-License-Identifier: GPL-2.0-only
"""Bounded source API over one owned pair-RPC thread and one native PID."""
from collections import deque
import json
from pathlib import Path
from threading import Condition,Thread
from pair_client import PairWorker


class HybridTransport:
    def __init__(self,cfg,*,capacity=3,timeout=5,on_process_started=None,worker=None):
        self.capacity,self.timeout=capacity,timeout;self._cv=Condition()
        self.front=None;self.jobs=deque();self.results=deque();self.inflight=0
        self.input_closed=False;self.finished=False;self.cancelled=False;self.error=None;self.thread=None
        self.counts=dict(submitted_sources=0,completed_full33_side=0,completed_lightning_front=0,
                         complete_hybrid_pairs=0,front_only_requests=0,max_inflight=0,cancelled_source_tickets=0)
        if worker is None:
            old=json.loads(Path(cfg['hybrid_lightning_config']).read_text())
            mapping=json.loads(Path(old['mapping']).read_text())
            command=[cfg['hybrid_pair_worker'],*cfg['tail4_worker_command'][1:],old['model'],
                     *[mapping['heads'][r]['name']for r in ('center','heatmap','offset','regression')]]
            worker=PairWorker(command,mapping,timeout=timeout,on_process_started=on_process_started)
        self.worker=worker

    def start(self):
        if self.thread is not None:return
        self.worker.start();self.thread=Thread(target=self._run,name='hybrid-single-pair-owner')
        self.thread.start()

    def submit(self,identity,pixels):
        identity=tuple(identity)
        with self._cv:
            if self.error:raise self.error
            if self.input_closed:raise RuntimeError('hybrid input closed')
            if self.inflight>=self.capacity:return False
            if identity[2]==1:
                if self.front is not None:return False
                if len(pixels)!=110592:raise ValueError('front actual Lightning input bytes')
                self.front=(identity,pixels)
            elif identity[2]==2:
                if self.front is None:raise ValueError('side has no original front pair ticket')
                if len(pixels)!=196608:raise ValueError('side actual Full33 input bytes')
                self.jobs.append((2,(identity,pixels),self.front));self.front=None
            else:raise ValueError('original physical source ID')
            self.inflight+=1;self.counts['submitted_sources']+=1
            self.counts['max_inflight']=max(self.counts['max_inflight'],self.inflight)
            self._cv.notify_all();return True

    def finish_pair_without_side(self,side_identity):
        # Cold/lost side ROI remains a genuine empty observation. Only the true
        # front CNN is executed; the native explicit kind3 returns zero side raw.
        with self._cv:
            if self.front is None:raise ValueError('empty side has no pending front ticket')
            self.jobs.append((3,(tuple(side_identity),b''),self.front));self.front=None;self._cv.notify_all()

    def _run(self):
        try:
            while True:
                with self._cv:
                    self._cv.wait_for(lambda:self.jobs or self.input_closed)
                    if not self.jobs:
                        if self.front is not None:raise ValueError('unpaired original front at EOF')
                        break
                    kind,side,front=self.jobs.popleft()
                value=self.worker.infer_pair(kind,side[0],front[0],side[1],front[1])
                with self._cv:
                    self.counts['completed_lightning_front']+=1
                    if kind==2:
                        self.counts['completed_full33_side']+=1;self.counts['complete_hybrid_pairs']+=1
                    else:self.counts['front_only_requests']+=1
                    if not self.cancelled:
                        self.results.append((front[0],dict(model_kind=2,raw=value['front_raw'])))
                        if kind==2:self.results.append((side[0],dict(model_kind=1,raw=value['side_raw'])))
                    self._cv.notify_all()
        except BaseException as error:
            with self._cv:self.error=error
        finally:
            try:self.worker.close()
            except BaseException as error:
                with self._cv:
                    if self.error is None:self.error=error
            with self._cv:self.finished=True;self._cv.notify_all()

    def take(self,timeout=0):
        with self._cv:
            self._cv.wait_for(lambda:self.results or self.error or self.finished,timeout)
            if self.results:
                result=self.results.popleft();self.inflight-=1;self._cv.notify_all();return result
            if self.error:raise self.error
            return None

    def finish_eof(self):
        with self._cv:self.input_closed=True;self._cv.notify_all()

    @property
    def eof(self):
        with self._cv:return self.finished and not self.results

    def drain(self,timeout=None):
        self.finish_eof();self.close();remaining=[]
        while True:
            result=self.take(0)
            if result is None:return remaining
            remaining.append(result)

    def cancel(self):
        with self._cv:
            self.cancelled=True;self.input_closed=True;self.front=None;self.jobs.clear();self.results.clear()
            self.counts['cancelled_source_tickets']+=self.inflight;self.inflight=0;self._cv.notify_all()

    def close(self):
        self.finish_eof()
        if self.thread is None:self.worker.close();self.finished=True;return
        self.thread.join(self.timeout+2)
        if self.thread.is_alive():raise RuntimeError('hybrid pair owner did not drain; not killing in-flight Tail')
        if self.error:raise self.error

    def stats(self):
        with self._cv:
            return dict(self.counts,capacity=self.capacity,inflight=self.inflight,queued_pairs=len(self.jobs),
                pending_front=self.front is not None,ready_sources=len(self.results),finished=self.finished,
                cancelled=self.cancelled,error=None if self.error is None else str(self.error),
                worker_footer=self.worker.footer,worker_pid=None if self.worker.process is None else self.worker.process.pid,
                native_processes=1,Runner_models=2,model_kinds=dict(side='Full33+Tail35',front='Lightning'))
