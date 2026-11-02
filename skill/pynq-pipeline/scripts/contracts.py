# SPDX-License-Identifier: GPL-2.0-only
"""Load the single validation boundary for a typed stream transaction."""
import json
from pathlib import Path

DTYPES={'uint8':(1,0,255),'int16':(2,-32768,32767),
        'uint16':(2,0,65535),'int32':(4,-2147483648,2147483647),'uint32':(4,0,4294967295)}

def load(contract_path, vector_path):
    path=Path(contract_path);contract=json.loads(path.read_text())
    vectors=json.loads(Path(vector_path).read_text())
    if contract.get('schema')!='stream-contract-v1':raise ValueError('Unsupported contract schema')
    for field in ('name','operation','dma','dtype','count','word_bytes','max_bytes'):
        if field not in contract:raise ValueError('Missing contract field: '+field)
    if contract['operation'] not in {'identity','affine'}:raise ValueError('Unsupported reference operation')
    if not isinstance(contract['dma'],str) or not contract['dma']:raise ValueError('dma must name an IP')
    if type(contract['max_bytes']) is not int or contract['max_bytes']<=0:raise ValueError('max_bytes must be positive')
    dtype=contract['dtype']
    if dtype not in DTYPES:raise ValueError('Unsupported dtype: '+dtype)
    width,minimum,maximum=DTYPES[dtype]
    if type(contract['count']) is not int or contract['count']<=0:raise ValueError('count must be positive')
    if type(contract['word_bytes']) is not int or (contract['word_bytes']<=0 or contract['word_bytes']&(contract['word_bytes']-1)):raise ValueError('word_bytes must be a positive power of two')
    size=width*contract['count']
    if size>contract['max_bytes'] or size%contract['word_bytes']:
        raise ValueError('Transfer size exceeds capacity or is not bus-word aligned')
    for field in ('input','expected'):
        data=vectors[field]
        if len(data)!=contract['count']:raise ValueError(field+' length differs from count')
        if any(type(x)is not int or not minimum<=x<=maximum for x in data):
            raise ValueError(field+' contains values outside '+dtype)
    control=contract.get('control')
    if contract['operation']=='affine':
        if control is None:raise ValueError('affine requires a control IP')
        if any(type(contract.get('parameters',{}).get(key)) is not int for key in ('scale','bias')):
            raise ValueError('affine requires integer scale and bias parameters')
    if control:
        for register in [*control.get('writes',[]),*([control['identity']] if 'identity' in control else []),
                         *([control['start']] if 'start' in control else [])]:
            if type(register['offset']) is not int or register['offset']<0 or register['offset']%4:
                raise ValueError('Register offsets must be aligned nonnegative bytes')
        for write in control.get('writes',[]):
            if 'parameter' in write and write['parameter'] not in contract.get('parameters',{}):
                raise ValueError('Register parameter is not defined')
    return contract,vectors,size


def write_value(contract, write):
    value=contract.get('parameters',{}).get(write.get('parameter')) if 'parameter' in write else write['value']
    return int(value)&0xffffffff
