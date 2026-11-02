"""Bounded little-endian native CNN worker v1 contract; no optional dependencies."""
from __future__ import annotations

import json
import struct
from typing import Any

MAGIC = b'PPCNN001'
HEADER = struct.Struct('<8sII')
FRAME = struct.Struct('<QQIIII')
INPUT_BYTES = 110592
OUTPUT_BYTES = 198144
ROLES = ('center', 'heatmap', 'offset', 'regression')
CHANNELS = (1, 17, 34, 34)


def request(frame_id: int, timestamp_ns: int, source_id: int,
            width: int, height: int, pixels: bytes) -> bytes:
    values = (frame_id, timestamp_ns, source_id, width, height)
    if any(type(value) is not int for value in values):
        raise ValueError('worker identity fields must be integers')
    if not (0 <= frame_id < 2**64 and 0 <= timestamp_ns < 2**64
            and source_id in (1, 2) and 0 < width <= 16384 and 0 < height <= 16384):
        raise ValueError('worker source identity outside contract')
    if len(pixels) != INPUT_BYTES:
        raise ValueError('worker RGB input byte count')
    payload = FRAME.pack(*values, INPUT_BYTES) + pixels
    return HEADER.pack(MAGIC, 1, len(payload)) + payload


def parse_header(data: bytes) -> tuple[int, int]:
    magic, kind, length = HEADER.unpack(data)
    if magic != MAGIC:
        raise ValueError('worker magic/version')
    bounds = {3: 16384, 2: FRAME.size + OUTPUT_BYTES, 4: 8}
    if kind not in bounds or not 0 < length <= bounds[kind]:
        raise ValueError('worker record kind/length')
    if kind in (2, 4) and length != bounds[kind]:
        raise ValueError('worker fixed record length')
    return kind, length


def _shape(value: Any, expected: list[int]) -> bool:
    return isinstance(value, list) and all(type(item) is int for item in value) and value == expected


def metadata(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get('schema') != 'posepriv.cnn_worker.v1':
        raise ValueError('worker metadata schema')
    if value.get('backend') != 'vart' or value.get('dtype') != 'int8' or value.get('layout') != 'NHWC':
        raise ValueError('worker backend/dtype/layout')
    inp = value.get('input')
    if not isinstance(inp, dict) or not _shape(inp.get('shape'), [1, 192, 192, 3]) or type(inp.get('bytes')) is not int or inp.get('bytes') != INPUT_BYTES or type(inp.get('fix_point')) is not int or inp['fix_point'] != 7:
        raise ValueError('worker input must be Q7 signed NHWC RGB')
    heads = value.get('heads')
    if not isinstance(heads, list) or len(heads) != 4:
        raise ValueError('worker head count')
    names: set[str] = set()
    for head, role, channels in zip(heads, ROLES, CHANNELS, strict=True):
        if not isinstance(head, dict):
            raise ValueError('worker head metadata')
        name = head.get('name')
        if not isinstance(name, str) or not 0 < len(name.encode('utf-8')) <= 512 or name in names:
            raise ValueError('worker head exact name')
        names.add(name)
        if head.get('role') != role or not _shape(head.get('shape'), [1, 48, 48, channels]) or type(head.get('bytes')) is not int or head.get('bytes') != 2304 * channels:
            raise ValueError('worker head role/shape/bytes')
        fix = head.get('fix_point')
        if type(fix) is not int or not -31 <= fix <= 31:
            raise ValueError('worker head fix_point')
    return value


def response(payload: bytes) -> tuple[tuple[int, int, int, int, int], dict[str, bytes]]:
    if len(payload) != FRAME.size + OUTPUT_BYTES:
        raise ValueError('worker response length')
    frame_id, ns, source, width, height, length = FRAME.unpack(payload[:FRAME.size])
    if length != OUTPUT_BYTES or source not in (1, 2) or not 0 < width <= 16384 or not 0 < height <= 16384:
        raise ValueError('worker response identity')
    offset = FRAME.size
    heads = {}
    for role, channels in zip(ROLES, CHANNELS, strict=True):
        length = 2304 * channels
        heads[role] = payload[offset:offset + length]
        offset += length
    return (frame_id, ns, source, width, height), heads
