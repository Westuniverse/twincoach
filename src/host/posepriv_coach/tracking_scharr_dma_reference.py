# SPDX-License-Identifier: MIT
"""Private Scharr DMA stream v1 golden; not a hardware driver or runtime backend."""
from dataclasses import dataclass
from typing import Iterable

from .tracking_pyramid_reference import scharr_gray


@dataclass(frozen=True)
class Axis128Beat:
    data: int
    keep: int
    last: bool


def _pixels(width: int, height: int) -> int:
    if type(width) is not int or type(height) is not int:
        raise ValueError("integer dimensions required")
    if not (1 <= width <= 1280 and 1 <= height <= 720):
        raise ValueError("dimensions outside Scharr v1 bounds")
    return width * height


def _pack(payload: bytes) -> tuple[Axis128Beat, ...]:
    return tuple(Axis128Beat(int.from_bytes(payload[start:start + 16], "little"),
                             (1 << len(payload[start:start + 16])) - 1,
                             start + 16 >= len(payload))
                 for start in range(0, len(payload), 16))


def _decode(beats: Iterable[Axis128Beat], length: int, *, zero_padding: bool) -> bytes:
    result = bytearray()
    count = 0
    for beat in beats:
        remaining = length - len(result)
        if remaining <= 0:
            raise ValueError("extra frame beat")
        valid = min(remaining, 16)
        if (type(beat.data) is not int or not 0 <= beat.data < 1 << 128
                or type(beat.keep) is not int or beat.keep != (1 << valid) - 1
                or type(beat.last) is not bool or beat.last != (remaining <= 16)):
            raise ValueError("invalid data/TKEEP/frame TLAST")
        if zero_padding and beat.data >> (valid * 8):
            raise ValueError("nonzero unused derivative output bytes")
        result.extend(beat.data.to_bytes(16, "little")[:valid])
        count += 1
    if len(result) != length or count == 0:
        raise ValueError("truncated frame")
    return bytes(result)


def pack_gray_frame(image: bytes, width: int, height: int) -> tuple[Axis128Beat, ...]:
    if len(image) != _pixels(width, height):
        raise ValueError("gray input length mismatch")
    return _pack(image)


def scharr_axis_reference(beats: Iterable[Axis128Beat], width: int,
                          height: int) -> tuple[Axis128Beat, ...]:
    """Validate whole input, then use the existing bit-exact integer golden."""
    image = _decode(beats, _pixels(width, height), zero_padding=False)
    return _pack(scharr_gray(image, width, height))


def decode_derivatives(beats: Iterable[Axis128Beat], width: int, height: int) -> bytes:
    return _decode(beats, _pixels(width, height) * 4, zero_padding=True)
