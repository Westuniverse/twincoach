"""Bit-exact references for optional video-enhancement branches.

These helpers deliberately stay independent from OpenCV and model runtimes so
that RTL and transport tests can share a small deterministic oracle.  The
nearest-neighbour scaler is a transport baseline, not a super-resolution
algorithm.
"""

from __future__ import annotations

ReadableBuffer = bytes | bytearray | memoryview


def midpoint_blend_u8(left: ReadableBuffer, right: ReadableBuffer) -> bytes:
    """Return the rounded midpoint of two equally sized unsigned-byte buffers.

    Each output byte is ``(left + right + 1) >> 1``.  The upward rounding rule
    is explicit so software and RTL agree for odd sums.
    """

    left_view = memoryview(left).cast("B")
    right_view = memoryview(right).cast("B")
    if len(left_view) != len(right_view):
        raise ValueError("midpoint inputs must have equal byte lengths")
    return bytes((lhs + rhs + 1) >> 1 for lhs, rhs in zip(left_view, right_view))


def nearest2x_rgb8_reference(
    frame: ReadableBuffer, *, width: int, height: int
) -> bytes:
    """Expand packed RGB8 pixels by 2x in both axes for transport validation.

    This is intentionally named a reference scaler rather than super
    resolution: it introduces no learned detail and exists only to validate
    dimensions, byte ordering, buffer sizes, and optional-branch plumbing.
    """

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    source = memoryview(frame).cast("B")
    row_bytes = width * 3
    expected_bytes = row_bytes * height
    if len(source) != expected_bytes:
        raise ValueError(
            f"RGB8 buffer has {len(source)} bytes; expected {expected_bytes} "
            f"for {width}x{height}"
        )

    output = bytearray(expected_bytes * 4)
    output_row_bytes = row_bytes * 2
    for y_index in range(height):
        input_row_start = y_index * row_bytes
        expanded_row = bytearray(output_row_bytes)
        for x_index in range(width):
            source_start = input_row_start + x_index * 3
            target_start = x_index * 6
            pixel = source[source_start : source_start + 3]
            expanded_row[target_start : target_start + 3] = pixel
            expanded_row[target_start + 3 : target_start + 6] = pixel

        first_output_row = (y_index * 2) * output_row_bytes
        second_output_row = first_output_row + output_row_bytes
        output[first_output_row : first_output_row + output_row_bytes] = expanded_row
        output[second_output_row : second_output_row + output_row_bytes] = expanded_row

    return bytes(output)


def pixel_shuffle2x_rgb8_reference(
    subpixels: ReadableBuffer, *, width: int, height: int
) -> bytes:
    """Rearrange packed 2x RGB8 subpixels into a raster-ordered RGB8 frame.

    Every low-resolution input location owns four RGB subpixels in row-major
    block order: ``top-left, top-right, bottom-left, bottom-right``.  In other
    words, each location contains 12 bytes::

        R00 G00 B00  R01 G01 B01  R10 G10 B10  R11 G11 B11

    The function implements only the deterministic depth-to-space operation.
    A convolution or learned model must produce ``subpixels`` before this
    mapping can be described as part of a super-resolution pipeline.
    """

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    source = memoryview(subpixels).cast("B")
    input_pixel_bytes = 4 * 3
    expected_bytes = width * height * input_pixel_bytes
    if len(source) != expected_bytes:
        raise ValueError(
            f"PixelShuffle buffer has {len(source)} bytes; expected "
            f"{expected_bytes} for {width}x{height}x12"
        )

    output_width = width * 2
    output_row_bytes = output_width * 3
    output = bytearray(expected_bytes)
    for y_index in range(height):
        top_row_start = (y_index * 2) * output_row_bytes
        bottom_row_start = top_row_start + output_row_bytes
        for x_index in range(width):
            source_start = (y_index * width + x_index) * input_pixel_bytes
            top_target = top_row_start + x_index * 6
            bottom_target = bottom_row_start + x_index * 6
            output[top_target : top_target + 6] = source[
                source_start : source_start + 6
            ]
            output[bottom_target : bottom_target + 6] = source[
                source_start + 6 : source_start + 12
            ]

    return bytes(output)


def packed_rgb8_bandwidth_bytes_per_second(
    *,
    width: int,
    height: int,
    fps: float,
    input_frames: int,
    output_width: int,
    output_height: int,
    output_frames: int,
) -> float:
    """Estimate payload-only RGB8 DDR traffic for one processing branch."""

    integer_fields = {
        "width": width,
        "height": height,
        "input_frames": input_frames,
        "output_width": output_width,
        "output_height": output_height,
        "output_frames": output_frames,
    }
    if any(value <= 0 for value in integer_fields.values()):
        invalid = ", ".join(name for name, value in integer_fields.items() if value <= 0)
        raise ValueError(f"positive dimensions and frame counts required: {invalid}")
    if fps <= 0:
        raise ValueError("fps must be positive")

    input_bytes = width * height * 3 * input_frames
    output_bytes = output_width * output_height * 3 * output_frames
    return (input_bytes + output_bytes) * fps


def minimum_dma_length_width(max_transfer_bytes: int) -> int:
    """Return the register width needed to encode one positive transfer size.

    An AXI DMA length field of width ``n`` represents at most ``2**n - 1``
    bytes, so exact powers of two require the next bit.
    """

    if max_transfer_bytes <= 0:
        raise ValueError("max_transfer_bytes must be positive")
    return max_transfer_bytes.bit_length()
