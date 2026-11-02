import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
SKILL=ROOT/'skill/pynq-pipeline'
sys.path.insert(0,str(SKILL/'scripts'))
from contracts import load
from backends import MockBackend,PynqBackend
from pipeline import execute
from report import render

class FakeBuffer(list):
    def __init__(self,count,events,name):super().__init__([0]*count);self.events=events;self.name=name
    def flush(self):self.events.append(self.name+'.flush')
    def invalidate(self):self.events.append(self.name+'.invalidate')
    def freebuffer(self):self.events.append(self.name+'.free')

class FakeChannel:
    def __init__(self,owner,name):self.owner=owner;self.name=name;self.running=True;self.buffer=None
    def transfer(self,buffer,nbytes):
        self.owner.events.append(self.name+'.submit');self.buffer=buffer
        if self.name=='tx':buffer.flush()
    def wait(self):
        self.transferred=len(self.buffer)*2
        if self.name=='rx' and self.owner.short:self.transferred-=2
        self.owner.events.append(self.name+'.wait')
        if self.name=='rx':
            if self.owner.fail:raise RuntimeError('Injected receive failure')
            scale=self.owner.registers.get(16,1);bias=self.owner.registers.get(24,0)
            if bias&0x80000000:bias-=1<<32
            self.buffer[:]=[x*scale+bias for x in self.owner.dma.sendchannel.buffer]
            self.buffer.invalidate()
    def stop(self):
        self.owner.events.append(self.name+'.stop')
        self.running=self.name=='rx' and self.owner.stuck

class FakePynq:
    def __init__(self,*,identity=0x4146464e,fail=False,stuck=False,sg=0,short=False):
        self.events=[];self.registers={};self.identity=identity;self.fail=fail;self.stuck=stuck
        self.short=short;self.loaded=True;self.sg=sg;self.allocations=0;self.download_requested=None
        self.dma=SimpleNamespace(sendchannel=FakeChannel(self,'tx'),recvchannel=FakeChannel(self,'rx'))
        self.ip=SimpleNamespace(read=self.read,write=self.write)
    def read(self,offset):self.events.append('id.read');return self.identity
    def write(self,offset,value):self.events.append('ip.write');self.registers[offset]=value
    def Overlay(self,path,download):
        self.download_requested=download
        return SimpleNamespace(ip_dict={'io/stream_dma':{'type':'xilinx.com:ip:axi_dma:7.1','parameters':{'c_include_sg':str(self.sg)}}},
            io=SimpleNamespace(stream_dma=self.dma),compute=SimpleNamespace(affine=self.ip),
            is_loaded=lambda:self.loaded,download=self.download)
    def download(self):self.events.append('program');self.loaded=True
    def allocate(self,shape,dtype):
        self.allocations+=1
        return FakeBuffer(shape[0],self.events,'tx' if self.allocations==1 else 'rx')

class SkillTests(unittest.TestCase):
    def case(self,name='affine'):
        return load(SKILL/'assets'/f'{name}.json',SKILL/'assets'/f'{name}-vectors.json')
    def backend(self,folder,api,**kwargs):
        bit=folder/'design.bit';bit.write_bytes(b'fixture')
        bit.with_suffix('.hwh').write_text('fixture')
        contract,_,_=self.case()
        return PynqBackend(contract,bit,api=api,**kwargs)
    def test_two_tasks_and_independent_golden(self):
        for name in ['copy','affine']:
            c,v,n=self.case(name);result=execute(c,v,n,MockBackend(c))
            self.assertTrue(result['passed']);self.assertFalse(result['hardware_executed'])
            self.assertFalse(result['board_performance_verified'])
        c,v,n=self.case();v['expected'][0]+=1
        result=execute(c,v,n,MockBackend(c))
        self.assertFalse(result['passed']);self.assertEqual(result['differences'][0]['index'],0)
    def test_contract_rejects_capacity_and_type(self):
        c,v,_=self.case()
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);cp=folder/'contract.json';vp=folder/'vectors.json'
            c['max_bytes']=4;cp.write_text(json.dumps(c));vp.write_text(json.dumps(v))
            with self.assertRaisesRegex(ValueError,'capacity'):load(cp,vp)
            c['max_bytes']=4096;v['input'][0]=100000;cp.write_text(json.dumps(c));vp.write_text(json.dumps(v))
            with self.assertRaisesRegex(ValueError,'outside'):load(cp,vp)
    def test_inspection_does_not_program_or_allocate(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq();backend=self.backend(Path(directory),api)
            report=backend.inspect()
            self.assertFalse(api.download_requested);self.assertFalse(report['hardware_executed'])
            self.assertEqual(api.events,[]);self.assertEqual(api.allocations,0)
    def test_adapter_order_cache_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq();backend=self.backend(Path(directory),api)
            c,v,n=self.case();actual=backend.transfer(v['input'],n)
            self.assertEqual(actual,v['expected'])
            self.assertLess(api.events.index('rx.submit'),api.events.index('tx.submit'))
            self.assertLess(api.events.index('rx.submit'),api.events.index('tx.flush'))
            self.assertEqual(api.events.count('tx.flush'),1)
            self.assertEqual(api.events.count('rx.invalidate'),1)
            self.assertLess(api.events.index('rx.wait'),api.events.index('rx.free'))
            self.assertEqual(backend.buffers,[])
    def test_identity_failure_precedes_writes_and_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq(identity=0);backend=self.backend(Path(directory),api);_,v,n=self.case()
            with self.assertRaisesRegex(RuntimeError,'identity'):backend.transfer(v['input'],n)
            self.assertEqual(api.allocations,0);self.assertNotIn('ip.write',api.events)
    def test_sg_dma_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq(sg=1);backend=self.backend(Path(directory),api);_,v,n=self.case()
            with self.assertRaisesRegex(ValueError,'simple'):backend.transfer(v['input'],n)
            self.assertEqual(api.allocations,0)
    def test_short_frame_does_not_pass_zero_filled_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq(short=True);backend=self.backend(Path(directory),api);_,v,n=self.case()
            with self.assertRaisesRegex(RuntimeError,'Received 10 bytes; expected 12'):
                backend.transfer(v['input'],n)
            self.assertEqual(backend.buffers,[])
            self.assertIn('rx.free',api.events)

    def test_error_stops_before_releasing(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq(fail=True);backend=self.backend(Path(directory),api);_,v,n=self.case()
            with self.assertRaisesRegex(RuntimeError,'receive'):backend.transfer(v['input'],n)
            self.assertLess(api.events.index('rx.stop'),api.events.index('tx.free'))
            self.assertEqual(backend.buffers,[])
    def test_unhalted_dma_retains_buffers(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakePynq(fail=True,stuck=True);backend=self.backend(Path(directory),api);_,v,n=self.case()
            with self.assertRaisesRegex(RuntimeError,'not halted'):backend.transfer(v['input'],n)
            self.assertEqual(len(backend.buffers),2);self.assertNotIn('tx.free',api.events)
            with self.assertRaisesRegex(RuntimeError,'retained'):backend.transfer(v['input'],n)
    def test_skills_run_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            for name in ['pynq-pipeline','pipeline-metrics']:
                shutil.copytree(ROOT/'skill'/name,folder/name,ignore=shutil.ignore_patterns('__pycache__'))
            local=folder/'pynq-pipeline';output=folder/'run.json';report=folder/'report.md';trace=folder/'trace.csv'
            command=[sys.executable,str(local/'scripts/pipeline.py'),'run','--contract',str(local/'assets/affine.json'),
                     '--vectors',str(local/'assets/affine-vectors.json'),'--output',str(output),
                     '--trace',str(trace),'--sample-id','source-1']
            result=subprocess.run(command,cwd=folder,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            artifact=json.loads(output.read_text());self.assertTrue(artifact['passed']);self.assertFalse(artifact['hardware_executed'])
            result=subprocess.run([sys.executable,str(local/'scripts/report.py'),str(output),'--output',str(report)],cwd=folder,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn('软件模型',report.read_text())
            metrics=folder/'pipeline-metrics'
            result=subprocess.run([sys.executable,str(metrics/'scripts/measure.py'),str(metrics/'assets/trace.csv'),
                '--origin-first','--start','0','--end','2'],cwd=folder,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout);self.assertEqual(value['fresh_outputs'],4)
            self.assertEqual(value['p95_source_age_ms'],300)
    def test_invalid_contract_produces_failure_record(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);contract=folder/'bad.json';output=folder/'result.json'
            contract.write_text('{"schema":"unknown"}')
            result=subprocess.run([sys.executable,str(SKILL/'scripts/pipeline.py'),'run','--contract',str(contract),
                '--vectors',str(SKILL/'assets/copy-vectors.json'),'--output',str(output)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            artifact=json.loads(output.read_text());self.assertFalse(artifact['passed']);self.assertFalse(artifact['hardware_executed'])
            self.assertIn('错误',render(artifact))

if __name__=='__main__':unittest.main()
