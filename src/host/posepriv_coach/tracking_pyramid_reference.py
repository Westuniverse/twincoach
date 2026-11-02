# SPDX-License-Identifier: MIT
"""Independent integer golden for optional tracking-pyramid hardware design.

Never selected by the tracker. Standard-library only; not a performance backend.
Flat gray headers do not inherit parent-ROI pixels. All returned pixels are owned.
"""

from dataclasses import dataclass
import struct

HALO = 21
MAX_LEVEL = 3
GAUSS5 = (1, 4, 6, 4, 1)


def _positive_int(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def reflect101(index: int, length: int) -> int:
    """Map arbitrary signed coordinates, including the degenerate length 1."""
    _positive_int(length, "length")
    if type(index) is not int:
        raise ValueError("index must be an integer")
    if length == 1:
        return 0
    period = 2 * (length - 1)
    position = index % period
    return min(position, period - position)


def _private_gray(data, width: int, height: int, stride: int | None) -> bytes:
    _positive_int(width, "width")
    _positive_int(height, "height")
    stride = width if stride is None else stride
    _positive_int(stride, "stride")
    if stride < width:
        raise ValueError("stride smaller than active row")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("one-dimensional contiguous byte buffer required")
    view = memoryview(data)
    # A foreign/ctypes/NumPy exporter can declare a fictitious accessible span.
    # Builtin roots and their views have a real allocation bound, without a raw pointer.
    if type(view.obj) not in (bytes, bytearray):
        raise ValueError("bytes or bytearray root allocation required")
    if view.ndim != 1 or view.itemsize != 1 or not view.c_contiguous:
        raise ValueError("one-dimensional contiguous byte buffer required")
    view = view.cast("B")
    if len(view) < (height - 1) * stride + width:
        raise ValueError("gray byte span too short")
    return b"".join(bytes(view[y * stride:y * stride + width]) for y in range(height))


def _scharr(gray: bytes, width: int, height: int) -> bytes:
    output = bytearray(width * height * 4)
    left = [reflect101(x - 1, width) for x in range(width)]
    right = [reflect101(x + 1, width) for x in range(width)]
    for y in range(height):
        top = reflect101(y - 1, height) * width
        middle = y * width
        bottom = reflect101(y + 1, height) * width
        for x, (xl, xr) in enumerate(zip(left, right)):
            dx = (3 * (gray[top + xr] - gray[top + xl]
                       + gray[bottom + xr] - gray[bottom + xl])
                  + 10 * (gray[middle + xr] - gray[middle + xl]))
            dy = (3 * (gray[bottom + xl] - gray[top + xl]
                       + gray[bottom + xr] - gray[top + xr])
                  + 10 * (gray[bottom + x] - gray[top + x]))
            struct.pack_into("<hh", output, (middle + x) * 4, dx, dy)
    return bytes(output)


def scharr_gray(data, width: int, height: int, *, stride: int | None = None) -> bytes:
    """Return row-major dx,dy signed-int16 little-endian bytes (no halo)."""
    return _scharr(_private_gray(data, width, height, stride), width, height)


def _pyr_down(gray: bytes, width: int, height: int) -> bytes:
    out_width, out_height = (width + 1) // 2, (height + 1) // 2
    taps = [tuple(reflect101(2 * x + k - 2, width) for k in range(5))
            for x in range(out_width)]
    horizontal = [
        [sum(weight * gray[y * width + x] for weight, x in zip(GAUSS5, column))
         for column in taps]
        for y in range(height)
    ]
    output = bytearray(out_width * out_height)
    for y in range(out_height):
        rows = tuple(reflect101(2 * y + k - 2, height) for k in range(5))
        for x in range(out_width):
            total = sum(weight * horizontal[row][x] for weight, row in zip(GAUSS5, rows))
            output[y * out_width + x] = (total + 128) >> 8
    return bytes(output)


def pyr_down_gray(data, width: int, height: int, *, stride: int | None = None) -> bytes:
    """Integer 5x5 ceil-halving reference; round only after both convolutions."""
    return _pyr_down(_private_gray(data, width, height, stride), width, height)


def _image_halo(image: bytes, width: int, height: int) -> bytes:
    columns = tuple(reflect101(x, width) for x in range(-HALO, width + HALO))
    rows = [bytes(image[y * width + x] for x in columns) for y in range(height)]
    return b"".join(rows[reflect101(y, height)] for y in range(-HALO, height + HALO))


def _derivative_halo(derivatives: bytes, width: int, height: int) -> bytes:
    row_bytes = (width + 2 * HALO) * 4
    border_row = bytes(row_bytes)
    border = bytes(HALO * 4)
    body = b"".join(border + derivatives[y * width * 4:(y + 1) * width * 4] + border
                    for y in range(height))
    return border_row * HALO + body + border_row * HALO


@dataclass(frozen=True)
class TrackingPyramidLevel:
    width: int
    height: int
    image: bytes
    derivatives: bytes

    def image_with_halo(self) -> bytes:
        return _image_halo(self.image, self.width, self.height)

    def derivatives_with_halo(self) -> bytes:
        return _derivative_halo(self.derivatives, self.width, self.height)


def pyramid_sizes(width: int, height: int) -> tuple[tuple[int, int], ...]:
    _positive_int(width, "width")
    _positive_int(height, "height")
    sizes = []
    for _ in range(MAX_LEVEL + 1):
        sizes.append((width, height))
        width, height = (width + 1) // 2, (height + 1) // 2
        if width <= HALO or height <= HALO:
            break
    return tuple(sizes)


def build_tracking_pyramid(data, width: int, height: int, *, stride: int | None = None
                           ) -> tuple[TrackingPyramidLevel, ...]:
    """Mirror the existing grayscale 21x21/maxLevel=3 numeric contract."""
    image = _private_gray(data, width, height, stride)
    levels = []
    sizes = pyramid_sizes(width, height)
    for index, (level_width, level_height) in enumerate(sizes):
        if index:
            image = _pyr_down(image, width, height)
        width, height = level_width, level_height
        levels.append(TrackingPyramidLevel(width, height, image, _scharr(image, width, height)))
    return tuple(levels)


def tracking_pyramid_layout(width: int, height: int) -> dict:
    """Byte budget, not DDR traffic/latency or an installed DMA ABI.

    Each complete image/derivative plane starts on 16 bytes; final padding is zero.
    Active input must first be packed if its actual row stride differs from width.
    """
    sizes = pyramid_sizes(width, height)
    planes = []
    offset, mat_bytes, active_bytes = 0, 0, 0
    for index, (columns, rows) in enumerate(sizes):
        for kind, pixel_bytes in (("image", 1), ("dx_dy_s16le", 4)):
            offset = (offset + 15) // 16 * 16
            row_bytes = (columns + 2 * HALO) * pixel_bytes
            count = row_bytes * (rows + 2 * HALO)
            active_bytes += columns * rows * pixel_bytes
            mat_bytes += count
            planes.append({"level": index, "kind": kind, "width": columns, "height": rows,
                           "halo_pixels": HALO, "row_bytes": row_bytes,
                           "offset": offset, "bytes": count})
            offset += count
    output_bytes = (offset + 15) // 16 * 16
    input_bytes = width * height
    return {"schema": "posepriv.tracking_pyramid_layout.v1", "planes": planes,
            "input_active_bytes": input_bytes, "input_frame_bytes": (input_bytes + 15) // 16 * 16,
            "output_active_bytes": active_bytes, "output_mat_bytes": mat_bytes,
            "output_halo_bytes": mat_bytes - active_bytes, "output_frame_bytes": output_bytes,
            "output_alignment_bytes": output_bytes - mat_bytes}
