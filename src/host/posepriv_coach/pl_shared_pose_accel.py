"""共享传输入口的离线批次契约；复用现有算术黄金参考。"""

from __future__ import annotations

from enum import IntEnum

from .pl_metrics import AxisBeat, reference_joint_output
from .pl_motion_summary import MOTION_SUMMARY_MAX_INPUT_BEATS, reference_motion_summary_output
from .rectified_stereo import (
    pack_rectified_stereo_result,
    reference_rectified_stereo_output,
    unpack_rectified_stereo_request,
)


class SharedPoseOperator(IntEnum):
    JOINT = 0
    MOTION_SUMMARY = 1
    RECTIFIED = 2
    TRIANGLE_LIGHTING = 3


_VIEW_RESERVED_MASK = bytes(value & 0xfe for value in range(256))


def validate_shared_pose_payload(
    operator: SharedPoseOperator | int, payload: bytes
) -> SharedPoseOperator:
    """校验不可变整批字节；长度定义末拍，不构造 AxisBeat 或重算算法。

    bytes 的切片、translate、strip 在 C 中扫描保留字段，避免逐拍 Python
    整数转换。原逐拍校验保留为独立一致性参考；内核仍独立校验输入。
    """
    if isinstance(operator, bool) or not isinstance(operator, int):
        raise ValueError("shared pose operator must be integer 0..3")
    try:
        selected = SharedPoseOperator(operator)
    except ValueError as error:
        raise ValueError("shared pose operator must be integer 0..3") from error
    if type(payload) is not bytes:
        raise ValueError("shared pose payload must be immutable native bytes")
    if not 16 <= len(payload) <= MOTION_SUMMARY_MAX_INPUT_BEATS * 16 or len(payload) % 16:
        raise ValueError("shared pose payload must contain 1..8192 complete 16-byte beats")
    if selected is SharedPoseOperator.TRIANGLE_LIGHTING:
        if len(payload) % 32:
            raise ValueError("shared lighting requires 1..4096 whole 32-byte faces")
        return selected  # Every coordinate/RGB/id bit is a defined field.
    if payload[15::16].strip(b"\0"):
        raise ValueError("shared pose input reserved bits must be zero")
    if selected is not SharedPoseOperator.RECTIFIED:
        views = payload[13::16]
        if payload[14::16].strip(b"\0") or views.translate(_VIEW_RESERVED_MASK).strip(b"\0"):
            raise ValueError("shared pose input reserved bits must be zero")
        if selected is SharedPoseOperator.MOTION_SUMMARY and views.strip(views[:1]):
            raise ValueError("shared pose summary input must use one view")
    return selected


def validate_shared_pose_input(
    operator: SharedPoseOperator | int, input_beats: tuple[AxisBeat, ...]
) -> SharedPoseOperator:
    """只校验，不计算黄金结果；真实传输不在 CPU 重算 PL 算术。"""

    if isinstance(operator, bool) or not isinstance(operator, int):
        raise ValueError("shared pose operator must be integer 0..3")
    try:
        selected = SharedPoseOperator(operator)
    except ValueError as error:
        raise ValueError("shared pose operator must be integer 0..3") from error
    if not input_beats or len(input_beats) > MOTION_SUMMARY_MAX_INPUT_BEATS:
        raise ValueError("shared pose batch must contain 1..8192 beats")
    reserved_lsb = 120 if selected is SharedPoseOperator.RECTIFIED else 105
    for index, beat in enumerate(input_beats):
        if len(beat.data) != 16:
            raise ValueError("shared pose input must contain complete 16-byte beats")
        if not isinstance(beat.last, bool) or beat.last != (index == len(input_beats) - 1):
            raise ValueError("shared pose TLAST must occur only on the final beat")
        if selected is not SharedPoseOperator.TRIANGLE_LIGHTING and int.from_bytes(beat.data, "little") >> reserved_lsb:
            raise ValueError("shared pose input reserved bits must be zero")
    if selected is SharedPoseOperator.TRIANGLE_LIGHTING and len(input_beats) % 2:
        raise ValueError("shared lighting requires 1..4096 whole 32-byte faces")
    if selected is SharedPoseOperator.MOTION_SUMMARY:
        views = {(int.from_bytes(beat.data, "little") >> 104) & 1 for beat in input_beats}
        if len(views) != 1:
            raise ValueError("shared pose summary input must use one view")
    return selected


def reference_shared_pose_output(
    operator: SharedPoseOperator | int, input_beats: tuple[AxisBeat, ...]
) -> tuple[AxisBeat, ...]:
    """校验完整批次，复用现有黄金算术；不执行 MMIO/DMA。"""
    selected = validate_shared_pose_input(operator, input_beats)

    if selected is SharedPoseOperator.JOINT:
        return tuple(reference_joint_output(beat) for beat in input_beats)
    if selected is SharedPoseOperator.MOTION_SUMMARY:
        return (reference_motion_summary_output(input_beats),)
    if selected is SharedPoseOperator.TRIANGLE_LIGHTING:
        from .avatar_triangle_lighting_packet import software_reference_output_batch
        results = []
        # Low-level packets are ordered and may repeat IDs. The display codec
        # separately requires unique IDs; no cross/sqrt is added to transport.
        for index in range(0, len(input_beats), 2):
            payload = input_beats[index].data + input_beats[index+1].data
            results.append(AxisBeat(software_reference_output_batch(payload), input_beats[index+1].last))
        return tuple(results)
    results = []
    for beat in input_beats:
        request = unpack_rectified_stereo_request(beat.data, last=beat.last)
        result = reference_rectified_stereo_output(request)
        results.append(AxisBeat(pack_rectified_stereo_result(result), result.last))
    return tuple(results)
