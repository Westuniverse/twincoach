"""Chinese presentation adapter for explainable exercise feedback."""

from __future__ import annotations

from .session import CorrectionDirection, FeedbackCue, SquatSessionUpdate


_CUE_TEXT = {
    ("side_depth_ratio", CorrectionDirection.BELOW): "下蹲位置偏低，最低点可以稍微提高。",
    ("side_depth_ratio", CorrectionDirection.ABOVE): "下蹲深度不足，再向下坐一些。",
    ("side_trunk_angle_deg", CorrectionDirection.BELOW): "躯干前倾不足，保持背部稳定并适当前倾。",
    ("side_trunk_angle_deg", CorrectionDirection.ABOVE): "躯干前倾过多，抬起胸口并保持背部稳定。",
    ("front_knee_symmetry", CorrectionDirection.BELOW): "左右膝弯曲不对称，左侧弯曲更多。",
    ("front_knee_symmetry", CorrectionDirection.ABOVE): "左右膝弯曲不对称，右侧弯曲更多。",
    ("tempo_seconds", CorrectionDirection.BELOW): "这一组动作偏快，下一次放慢一些。",
    ("tempo_seconds", CorrectionDirection.ABOVE): "这一组动作偏慢，下一次保持更连贯。",
}


def format_feedback_cue_zh(cue: FeedbackCue) -> str:
    """Translate one language-neutral correction into concise spoken Chinese."""

    text = _CUE_TEXT.get(
        (cue.metric, cue.direction),
        "有一项动作指标偏低，请检查姿势后再试一次。",
    )
    if cue.sustained_seconds:
        phases = {"descending": "下蹲", "bottom": "底部", "ascending": "起身"}
        stage = "、".join(phases.get(phase, phase) for phase in cue.phases)
        return f"{stage}阶段连续偏差{cue.sustained_seconds:.1f}秒：{text}"
    return text


def format_completed_squat_zh(update: SquatSessionUpdate) -> str | None:
    """Create one TTS-ready sentence only after a valid completed repetition."""

    if not update.repetition_completed or update.repetition_score is None:
        return None
    score = update.repetition_score
    if not score.valid or score.conformance_percent is None:
        return f"第{update.repetition_count}次深蹲完成，但关键点不足，暂不评分。"

    message = (
        f"第{update.repetition_count}次深蹲完成，"
        f"动作规范度{score.conformance_percent:.0f}分，"
        f"观测置信度{score.confidence_percent:.0f}%。"
    )
    if update.cues:
        message += format_feedback_cue_zh(update.cues[0])
    else:
        message += "动作保持得很好。"
    return message
