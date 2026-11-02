"""PPT4SM01: fixed raw tensors in four shared slots, short source tickets."""
import json
import struct

from combined_protocol import INPUT_BYTES, OUTPUT_BYTES, ROLES, SIZES, WIRE_ROLES, SHAPES, QS

MAGIC=b'PPT4SM01'
HEADER=struct.Struct('<8sII')
FRAME=struct.Struct('<QQIIII')
SLOT=struct.Struct('<IQ') # ring index plus monotonically increasing lease generation
TICKET_BYTES=FRAME.size+SLOT.size
RING_SLOTS=4
INPUT_OFFSET=0
OUTPUT_OFFSET=INPUT_BYTES
SLOT_STRIDE=(INPUT_BYTES+OUTPUT_BYTES+4095)&~4095
RING_BYTES=RING_SLOTS*SLOT_STRIDE


def request(identity,index,generation):
    limits=(2**64,2**64,2**32,2**32,2**32)
    if len(identity)!=5 or any(type(v)is not int or not 0<=v<limit for v,limit in zip(identity,limits)):
        raise ValueError('shared tensor source identity must retain its unsigned wire domain')
    if not 0<=index<RING_SLOTS or not 0<generation<2**64:
        raise ValueError('shared tensor ring slot/generation')
    payload=FRAME.pack(*identity,INPUT_BYTES)+SLOT.pack(index,generation)
    return HEADER.pack(MAGIC,1,TICKET_BYTES)+payload


def parse_header(data):
    magic,kind,length=HEADER.unpack(data)
    if magic!=MAGIC:raise ValueError('shared tensor magic/version')
    bounds={3:16384,2:TICKET_BYTES,4:8}
    if kind not in bounds or not 0<length<=bounds[kind]:raise ValueError('shared tensor kind/length')
    if kind in(2,4)and length!=bounds[kind]:raise ValueError('shared tensor fixed record length')
    return kind,length


def metadata(payload,*,allow_mock=False):
    value=json.loads(payload)
    backend=value.get('backend')
    valid=('vart_tail4_linux_memfd','backend_mock_memfd')if allow_mock else('vart_tail4_linux_memfd',)
    if value.get('schema')!='posepriv.tail4_shm_worker.v1' or backend not in valid:
        raise ValueError('shared tensor metadata schema/backend')
    native=backend=='vart_tail4_linux_memfd'
    expected_input=dict(name='mp_pose_input/aquant',dtype='int8',shape=[1,256,256,3],fix_point=7,bytes=INPUT_BYTES)
    expected_outputs=[dict(role=r,dtype='int8'if i==3 else'int16',shape=s,fix_point=q,bytes=n)
                      for i,(r,s,q,n)in enumerate(zip(WIRE_ROLES,SHAPES,QS,SIZES))]
    expected_ring=dict(slots=RING_SLOTS,stride=SLOT_STRIDE,input_offset=INPUT_OFFSET,
                       output_offset=OUTPUT_OFFSET,bytes=RING_BYTES,ticket_bytes=TICKET_BYTES)
    if(value.get('input')!=expected_input or value.get('outputs')!=expected_outputs
       or value.get('slots')!=2 or value.get('runner_creations')!=int(native)
       or value.get('tail_fd_opens')!=int(native)or value.get('ring')!=expected_ring):
        raise ValueError('shared tensor format/ownership/ring contract changed')
    return value


def response(payload):
    if len(payload)!=TICKET_BYTES:raise ValueError('shared tensor ticket length')
    *identity,length=FRAME.unpack_from(payload)
    index,generation=SLOT.unpack_from(payload,FRAME.size)
    if length!=OUTPUT_BYTES or not 0<=index<RING_SLOTS or not generation:
        raise ValueError('shared tensor response ring/size/generation')
    return tuple(identity),index,generation
