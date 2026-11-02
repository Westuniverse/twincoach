# SPDX-License-Identifier: MIT
"""Bounded CPU-test fixture, not a production or DMA ABI."""

from dataclasses import dataclass
import struct

from .tracking_pyramid_reference import HALO, build_tracking_pyramid, pyramid_sizes

MAGIC = b"PPTRGOLD"
HEADER = struct.Struct("<8s7I")
PLANE_HEADER = struct.Struct("<4I")
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class PyramidFixture:
    width: int
    height: int
    stride: int
    input_bytes: bytes
    planes: tuple[tuple[int, int, bytes, bytes], ...]


def _sizes(width, height, stride):
    if (type(width) is not int or type(height) is not int or type(stride) is not int
            or not 1 <= width <= 4096 or not 1 <= height <= 2160
            or not width <= stride <= width + 64):
        raise ValueError("fixture dimensions or stride invalid")
    return pyramid_sizes(width, height)


def encode_pyramid_fixture(data, width: int, height: int, *, stride: int) -> bytes:
    sizes = _sizes(width, height, stride)
    span = (height - 1) * stride + width
    expected_size = HEADER.size + span + len(sizes) * PLANE_HEADER.size + sum(
        (w + 2 * HALO) * (h + 2 * HALO) * 5 for w, h in sizes)
    if expected_size > MAX_BYTES:
        raise ValueError("fixture size limit exceeded")
    levels = build_tracking_pyramid(data, width, height, stride=stride)
    # The reference has already checked the real builtin root allocation.
    source = bytes(memoryview(data).cast("B")[:span])
    parts = [HEADER.pack(MAGIC, 1, width, height, stride, span, len(sizes), 0), source]
    for level in levels:
        image, derivatives = level.image_with_halo(), level.derivatives_with_halo()
        parts.extend((PLANE_HEADER.pack(level.width, level.height, len(image), len(derivatives)),
                      image, derivatives))
    output = b"".join(parts)
    if len(output) != expected_size:
        raise AssertionError("fixture encoder length differs")
    return output


def decode_pyramid_fixture(data: bytes) -> PyramidFixture:
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise ValueError("bounded immutable fixture bytes required")
    if len(data) < HEADER.size:
        raise ValueError("fixture truncated")
    magic, version, width, height, stride, span, count, reserved = HEADER.unpack_from(data)
    if magic != MAGIC or version != 1 or reserved:
        raise ValueError("fixture header invalid")
    sizes = _sizes(width, height, stride)
    if span != (height - 1) * stride + width or count != len(sizes):
        raise ValueError("fixture span or level count invalid")
    offset = HEADER.size

    def take(length):
        nonlocal offset
        if length > len(data) - offset:
            raise ValueError("fixture truncated")
        value = data[offset:offset + length]
        offset += length
        return value

    source = take(span)
    planes = []
    for columns, rows in sizes:
        lw, lh, image_len, derivative_len = PLANE_HEADER.unpack(take(PLANE_HEADER.size))
        expected = (columns + 2 * HALO) * (rows + 2 * HALO)
        if (lw, lh, image_len, derivative_len) != (columns, rows, expected, expected * 4):
            raise ValueError("fixture plane dimensions or lengths invalid")
        planes.append((lw, lh, take(image_len), take(derivative_len)))
    if offset != len(data):
        raise ValueError("fixture trailing bytes")
    return PyramidFixture(width, height, stride, source, tuple(planes))


def negative_fixtures(valid: bytes) -> dict[str, tuple[bytes, str]]:
    """Strict-parser negatives and four actual comparison mutations at level zero."""
    fixture = decode_pyramid_fixture(valid)
    result = {}

    def field(name, offset, value):
        altered = bytearray(valid)
        struct.pack_into("<I", altered, offset, value)
        result[name] = (bytes(altered), "fixture")

    result["bad_magic"] = (b"X" + valid[1:], "fixture")
    field("bad_version", 8, 2)
    field("bad_reserved", 32, 1)
    field("bad_width", 12, 0)
    field("bad_stride", 20, fixture.width - 1)
    field("bad_span", 24, len(fixture.input_bytes) - 1)
    field("bad_levels", 28, len(fixture.planes) + 1)
    plane = HEADER.size + len(fixture.input_bytes)
    field("bad_plane_length", plane + 8, len(fixture.planes[0][2]) + 1)
    result["truncated"] = (valid[:-1], "fixture")
    result["trailing"] = (valid + b"\0", "fixture")
    image = plane + PLANE_HEADER.size
    derivatives = image + len(fixture.planes[0][2])
    row_width = fixture.width + 2 * HALO
    for name, offset in (("image_halo", image),
                         ("image_inner", image + HALO * row_width + HALO),
                         ("derivative_halo", derivatives),
                         ("derivative_inner", derivatives + (HALO * row_width + HALO) * 4)):
        altered = bytearray(valid)
        altered[offset] ^= 1
        result[name] = (bytes(altered), "golden mismatch")
    return result
