"""坐标请求直接到时序摘要的独立 MOTION-SUMMARY v1 契约。"""

from __future__ import annotations

from .pl_metrics import AxisBeat
from .pl_motion import reference_motion_output, reference_motion_stats_output


MOTION_SUMMARY_MAX_INPUT_BEATS = 8192  # Existing candidate driver: 128 KiB.


def reference_motion_summary_output(input_beats: tuple[AxisBeat, ...]) -> AxisBeat:
    """返回原始帧差分摘要，不代替不等间隔时间归一化。"""

    if not input_beats or len(input_beats) > MOTION_SUMMARY_MAX_INPUT_BEATS:
        raise ValueError("motion summary batch must contain 1..8192 beats")
    for beat in input_beats:
        if len(beat.data) != 16:
            raise ValueError("motion summary input must contain complete 16-byte beats")
        if int.from_bytes(beat.data, "little") >> 105:
            raise ValueError("motion summary input reserved bits must be zero")
    # Existing golden rejects early/missing TLAST and mixed views. Coordinates
    # are decoded as signed int16; summary arithmetic retains full integer width.
    return reference_motion_stats_output(tuple(reference_motion_output(beat) for beat in input_beats))
