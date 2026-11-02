"""MOVENET-RGB-I8v1: already resized RGB8, fixed model input fix_point=6."""

from .pl_metrics import AXIS_BEAT_BYTES, AxisBeat


def quantize_rgb_u8_fix6(value: int) -> int:
    """Exact 256-entry mapping of the current normalized MoveNet input LUT."""
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError('RGB input must be an unsigned byte integer')
    return (value + 1) // 2 - 64


def reference_rgb_int8_output(beat: AxisBeat) -> AxisBeat:
    """Keep contiguous byte order and caller-owned whole-frame TLAST."""
    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError('RGB quantizer requires a complete 16-byte AXI beat')
    return AxisBeat(bytes(quantize_rgb_u8_fix6(value) & 255 for value in beat.data), beat.last)


__all__ = ['quantize_rgb_u8_fix6', 'reference_rgb_int8_output']
