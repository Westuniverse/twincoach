"""Small HDMI presentation payload carried with the scored source pair."""
from .session import CorrectionDirection


_CUES = {
    ('side_depth_ratio', CorrectionDirection.ABOVE): '下蹲幅度偏小，保持躯干稳定，适当增加屈髋屈膝幅度',
    ('side_depth_ratio', CorrectionDirection.BELOW): '下蹲超出当前目标深度，适当减小下蹲幅度',
    ('side_trunk_angle_deg', CorrectionDirection.ABOVE): '躯干前倾偏大，收紧核心，保持胸背稳定',
    ('side_trunk_angle_deg', CorrectionDirection.BELOW): '髋部适度后移，使躯干前倾与下蹲深度协调',
    ('front_knee_symmetry', CorrectionDirection.BELOW): '左侧屈膝幅度偏大，调整两侧屈伸幅度，保持动作对称',
    ('front_knee_symmetry', CorrectionDirection.ABOVE): '右侧屈膝幅度偏大，调整两侧屈伸幅度，保持动作对称',
    ('tempo_seconds', CorrectionDirection.BELOW): '动作节奏偏快，控制下蹲速度，平稳起身',
    ('tempo_seconds', CorrectionDirection.ABOVE): '动作节奏偏慢，保持下蹲与起身衔接连贯',
}
_PHASES = {'unknown': '等待入镜', 'standing': '站立', 'descending': '下蹲',
           'bottom': '最低点', 'ascending': '起身'}


class CompactCoach:
    def __init__(self):
        self.completed_hint = None
        self.payload = {'phase': '等待入镜', 'hint': '请保持全身入镜'}

    def update(self, update):
        if update.repetition_completed:
            score = update.repetition_score
            self.completed_hint = (
                '本次有效观测不足，请确保双视角全身可见' if score is None or not score.valid else
                _CUES.get((update.cues[0].metric, update.cues[0].direction), '保持躯干稳定，控制动作幅度与节奏')
                if update.cues else '本次动作符合当前评分要求，保持动作幅度与节奏')
        if self.completed_hint:
            hint = '下次建议：' + self.completed_hint
        elif not update.live_score.valid:
            hint = '当前有效观测不足，请确保双视角全身可见'
        elif update.live_cues:
            cue = update.live_cues[0]
            hint = _CUES.get((cue.metric, cue.direction), '保持躯干稳定，控制动作幅度与节奏')
        else:
            hint = '髋膝协调屈伸，控制下蹲速度，平稳起身'
        self.payload = {'phase': _PHASES.get(update.phase.value, '等待入镜'), 'hint': hint}


def summary_text(count, score, coach, *, enabled=True):
    if not enabled:
        return '姿态预览 · 评分未开启'
    value = '--' if score is None or not score.valid or score.conformance_percent is None else f'{score.conformance_percent:.0f}'
    return f"深蹲  {count} 次     上次得分  {value}     {coach.get('phase', '等待入镜')}"
