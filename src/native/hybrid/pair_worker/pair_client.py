# SPDX-License-Identifier: GPL-2.0-only
"""One owned pair request; two typed source outputs, no tensor masquerading."""
import json
import os
import struct
import subprocess
import tempfile
from time import monotonic

from posepriv_coach.movenet_cnn_worker import _WorkerProcess

MAGIC=b'PPHYPR01'
HEADER=struct.Struct('<8sII')
FRAME=struct.Struct('<QQIIII')
PAIR=struct.Struct('<II')
TIMES=struct.Struct('<QQQQQ')
FULL_INPUT=196608
FULL_OUTPUT=160370
LIGHT_INPUT=110592
LIGHT_OUTPUT=198144


class PairWorker(_WorkerProcess):
    def __init__(self,command,mapping,*,timeout=5,allow_mock=False,on_process_started=None):
        super().__init__(command,mapping,timeout,on_process_started=on_process_started)
        self.pairs=0;self.allow_mock=allow_mock;self.footer=None

    def _record(self,deadline):
        magic,kind,length=HEADER.unpack(self._read(16,deadline))
        limits={3:16384,2:112+FULL_OUTPUT+LIGHT_OUTPUT,4:32}
        if magic!=MAGIC or kind not in limits or not 0<length<=limits[kind]:
            raise ValueError('hybrid typed header/length')
        return kind,self._read(length,deadline)

    def _start(self,deadline):
        self._stderr=tempfile.TemporaryFile(mode='w+b')
        self.process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=self._stderr,bufsize=0,start_new_session=True)
        self._pgid=self.process.pid
        if self._on_process_started is not None:self._on_process_started(self.process.pid)
        os.set_blocking(self.process.stdin.fileno(),False);os.set_blocking(self.process.stdout.fileno(),False)
        kind,data=self._record(deadline)
        if kind!=3:raise ValueError('hybrid worker missing metadata')
        self.metadata=json.loads(data)
        allowed=['vart_full33_lightning_one_process']+(['host_mock_no_NN']if self.allow_mock else[])
        if (self.metadata.get('schema')!='posepriv.hybrid_pair_worker.v1' or
                self.metadata.get('backend')not in allowed or self.metadata.get('full_input_bytes')!=FULL_INPUT or
                self.metadata.get('full_output_bytes')!=FULL_OUTPUT or self.metadata.get('lightning_input_bytes')!=LIGHT_INPUT or
                self.metadata.get('lightning_output_bytes')!=LIGHT_OUTPUT):
            raise ValueError('hybrid model ABI differs')

    def start(self):
        if self.process is None:self._start(monotonic()+self.timeout)

    def infer_pair(self,kind,side_identity,front_identity,side_pixels,front_pixels):
        self.start()
        expected=FULL_INPUT if kind==1 else LIGHT_INPUT if kind in(2,3)else 0
        side_input=0 if kind==3 else FULL_INPUT;side_output=0 if kind==3 else FULL_OUTPUT
        if not expected or len(side_pixels)!=side_input or len(front_pixels)!=expected:
            raise ValueError('explicit hybrid model/input length')
        if side_identity[2]!=2 or front_identity[2]!=1:
            raise ValueError('original physical side/front pair required')
        prefix=PAIR.pack(kind,self.pairs)+FRAME.pack(*side_identity,side_input)+FRAME.pack(*front_identity,expected)
        data=prefix+side_pixels+front_pixels;deadline=monotonic()+self.timeout
        self._write(HEADER.pack(MAGIC,1,len(data))+data,deadline)
        record,response=self._record(deadline)
        if record!=2 or len(response)!=112+side_output+(FULL_OUTPUT if kind==1 else LIGHT_OUTPUT):
            raise ValueError('incomplete or wrong-model hybrid pair')
        k,ordinal=PAIR.unpack_from(response)
        *side,side_bytes=FRAME.unpack_from(response,8)
        *front,front_bytes=FRAME.unpack_from(response,40)
        if ((k,ordinal)!=(kind,self.pairs)or tuple(side)!=tuple(side_identity)or tuple(front)!=tuple(front_identity)
                or side_bytes!=side_output or front_bytes!=(FULL_OUTPUT if kind==1 else LIGHT_OUTPUT)):
            raise ValueError('hybrid response original identity/model mismatch')
        self.pairs+=1
        return dict(kind=kind,side_raw=response[112:112+side_output],front_raw=response[112+side_output:],
                    native_ns=dict(zip(('side_prefix','side_tail','front_CNN','front_tail','pair_NN'),TIMES.unpack_from(response,72))))

    def close(self):
        if self.closed:return
        try:
            if self.process is not None:
                self.process.stdin.close();kind,payload=self._record(monotonic()+self.timeout)
                if kind!=4 or len(payload)!=32:raise ValueError('hybrid missing complete footer')
                self.footer=dict(zip(('pairs','Full33_DPU','Lightning_DPU','Tail35'),struct.unpack('<QQQQ',payload)))
                if self.footer['pairs']!=self.pairs:raise ValueError('hybrid footer pair count')
                if self.process.wait(timeout=self.timeout)!=0:raise ValueError('hybrid worker exited nonzero')
                if self.process.stdout.read(1)!=b'':raise ValueError('hybrid bytes after footer')
                self.process.stdout.close();self._pgid=None;self.complete=True
                self._stderr.seek(0);self._stderr_tail=self._stderr.read().decode('utf8','replace')
                self._stderr.close();self._stderr=None
        except BaseException as exc:raise self._fail(exc)
        finally:
            self.closed=True

    def diagnostic_stderr(self):
        if self._stderr is None:return self._stderr_tail
        self._stderr.seek(0);return self._stderr.read().decode('utf8','replace')
