# SPDX-License-Identifier: GPL-2.0-only
"""Reference backend and PYNQ adapter with explicit buffer ownership."""
from pathlib import Path

class MockBackend:
    name='mock'
    hardware=False
    def __init__(self, contract):self.contract=contract;self.events=[]
    def inspect(self):
        return {'backend':self.name,'hardware_executed':False,'dma':self.contract['dma'],
                'control':self.contract.get('control',{}).get('ip'),'metadata_source':'contract'}
    def transfer(self, values, size):
        self.events+=['configure','receive_submit','kernel_start','send_submit','send_complete','receive_complete','release']
        if self.contract['operation']=='identity':return list(values)
        p=self.contract['parameters']
        return [x*p['scale']+p['bias'] for x in values]

class PynqBackend:
    name='pynq'
    hardware=True
    def __init__(self, contract, bitstream, *, program=False, api=None):
        if api is None:
            import pynq as api
        self.api=api;self.contract=contract;self.events=[];self.buffers=[]
        bitstream=Path(bitstream).resolve()
        if not bitstream.is_file() or not bitstream.with_suffix('.hwh').is_file():
            raise ValueError('Matching .bit and .hwh files are required')
        self.overlay=api.Overlay(str(bitstream),download=False)
        if program:self.overlay.download()
    def inspect(self):
        ips={name:{k:info[k] for k in ('type','phys_addr','addr_range') if k in info}
             for name,info in self.overlay.ip_dict.items()}
        return {'backend':self.name,'hardware_executed':False,'loaded':bool(self.overlay.is_loaded()),
                'ips':ips,'metadata_source':'hwh'}
    def resolve(self, name):
        value=self.overlay
        for part in name.split('/'):value=getattr(value,part)
        return value
    def transfer(self, values, size):
        if self.buffers:raise RuntimeError('Buffers are still retained; recover the DMA before reuse')
        if not self.overlay.is_loaded():raise RuntimeError('Overlay is not loaded; programming must be explicit')
        metadata=self.overlay.ip_dict[self.contract['dma']]
        parameters={str(k).lower():v for k,v in metadata.get('parameters',{}).items()}
        if ':axi_dma:' not in metadata.get('type','') or int(str(parameters.get('c_include_sg','-1')),0)!=0:
            raise ValueError('Only an explicitly described simple AXI DMA is supported')
        control=self.contract.get('control')
        ip=self.resolve(control['ip']) if control else None
        if control and 'identity' in control:
            identity=control['identity'];mask=identity.get('mask',0xffffffff)
            if ip.read(identity['offset'])&mask != identity['value']&mask:
                raise RuntimeError('IP identity mismatch before writes or DMA allocation')
        dma=self.resolve(self.contract['dma'])
        if control:
            from contracts import write_value
            for write in control.get('writes',[]):ip.write(write['offset'],write_value(self.contract,write))
        self.events.append('configure')
        tx=self.api.allocate(shape=(self.contract['count'],),dtype=self.contract['dtype'])
        self.buffers.append(tx)
        try:
            rx=self.api.allocate(shape=(self.contract['count'],),dtype=self.contract['dtype'])
            self.buffers.append(rx)
        except BaseException:
            tx.freebuffer();self.buffers=[];raise
        active=False
        try:
            tx[:]=values
            # PYNQ's simple DMA driver flushes TX on transfer and invalidates RX on wait.
            # Do not duplicate cache operations at this layer.
            active=True
            dma.recvchannel.transfer(rx,nbytes=size);self.events.append('receive_submit')
            if control and 'start' in control:
                ip.write(control['start']['offset'],control['start']['value'])
            self.events.append('kernel_start')
            dma.sendchannel.transfer(tx,nbytes=size);self.events.append('send_submit')
            dma.sendchannel.wait();self.events.append('send_complete')
            dma.recvchannel.wait();self.events.append('receive_complete')
            active=False
            if dma.recvchannel.transferred!=size:
                raise RuntimeError(f'Received {dma.recvchannel.transferred} bytes; expected {size}')
            return [int(x) for x in rx]
        finally:
            # stop() follows the vendor's blocking halt semantics. Never free a buffer
            # still owned by a channel; a second interruption requires board-side recovery.
            if active:
                dma.sendchannel.stop();dma.recvchannel.stop()
                if dma.sendchannel.running or dma.recvchannel.running:
                    raise RuntimeError('DMA is not halted; buffers remain retained by this session')
            for buffer in self.buffers:buffer.freebuffer()
            self.buffers=[];self.events.append('release')
