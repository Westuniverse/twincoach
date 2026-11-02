"""PPT4CW01: raw Full33 prefix+DPU/tail+PL framed transport (GPL-2.0-only)."""
from __future__ import annotations

import json
import struct

MAGIC = b'PPT4CW01'
HEADER = struct.Struct('<8sII')
FRAME = struct.Struct('<QQIIII')
INPUT_BYTES = 196608
OUTPUT_BYTES = 160370
ROLES = ('head195', 'world117', 'presence_logit', 'heat233')
SIZES = (390, 234, 2, 159744)
WIRE_ROLES = ('landmarks195', 'world117', 'presence_logit1', 'heat233')
SHAPES = ([1, 195], [1, 117], [1, 1], [1, 64, 64, 39])
QS = (5, 14, 8, 2)


def request(frame_id, timestamp_ns, source_id, width, height, pixels):
    identity = (frame_id, timestamp_ns, source_id, width, height)
    limits = (2**64, 2**64, 2**32, 2**32, 2**32)
    if any(type(v) is not int or not 0 <= v < limit for v, limit in zip(identity, limits)):
        raise ValueError('combined identity must retain its unsigned integer wire domain')
    if len(pixels) != INPUT_BYTES:
        raise ValueError('combined input must be 256x256x3 S8Q7 bytes')
    return HEADER.pack(MAGIC, 1, FRAME.size + INPUT_BYTES) + FRAME.pack(*identity, INPUT_BYTES) + pixels


def parse_header(data):
    magic, kind, length = HEADER.unpack(data)
    if magic != MAGIC:
        raise ValueError('combined magic/version')
    bounds = {3: 16384, 2: FRAME.size + OUTPUT_BYTES, 4: 8}
    if kind not in bounds or not 0 < length <= bounds[kind]:
        raise ValueError('combined record kind/length')
    if kind in (2, 4) and length != bounds[kind]:
        raise ValueError('combined fixed record length')
    return kind, length


def metadata(payload, *, allow_mock=False):
    value = json.loads(payload)
    backend = value.get('backend')
    if value.get('schema') != 'posepriv.tail4_combined_worker.v1' or backend not in (
            ('vart_tail4_linux', 'backend_mock') if allow_mock else ('vart_tail4_linux',)):
        raise ValueError('combined metadata schema/backend')
    expected_input = dict(name='mp_pose_input/aquant', dtype='int8', shape=[1, 256, 256, 3],
                          fix_point=7, bytes=INPUT_BYTES)
    expected_outputs = [dict(role=role, dtype='int8' if i == 3 else 'int16', shape=shape,
                             fix_point=q, bytes=size)
                        for i, (role, shape, q, size) in enumerate(zip(WIRE_ROLES, SHAPES, QS, SIZES))]
    native = backend == 'vart_tail4_linux'
    if (value.get('input') != expected_input or value.get('outputs') != expected_outputs
            or value.get('slots') != 2 or value.get('runner_creations') != int(native)
            or value.get('tail_fd_opens') != int(native)):
        raise ValueError('combined input/output/ownership contract changed')
    return value


def response(payload):
    if len(payload) != FRAME.size + OUTPUT_BYTES:
        raise ValueError('combined response length')
    *identity, length = FRAME.unpack_from(payload)
    if length != OUTPUT_BYTES:
        raise ValueError('combined response head bytes')
    offset = FRAME.size
    heads = {}
    for role, size in zip(ROLES, SIZES):
        heads[role] = payload[offset:offset + size]
        offset += size
    return tuple(identity), heads
