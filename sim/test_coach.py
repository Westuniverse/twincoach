import sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/host'))
from posepriv_coach.compact_coach import CompactCoach, summary_text
from posepriv_coach.session import CorrectionDirection, FeedbackCue
from posepriv_coach.exercises.squat_tracker import SquatPhase

def cue(metric='side_depth_ratio', direction=CorrectionDirection.ABOVE):
    return FeedbackCue(metric,direction,1.,50.,90.)

def update(*, valid=True, completed=False, cues=(), live_cues=()):
    return NS(repetition_completed=completed, repetition_score=NS(valid=valid),
              live_score=NS(valid=valid), cues=cues, live_cues=live_cues,
              phase=SquatPhase.DESCENDING)

class CoachTests(unittest.TestCase):
    def test_completed_correction_survives_following_pairs(self):
        coach=CompactCoach()
        coach.update(update(completed=True,cues=(cue(),)))
        coach.update(update())
        self.assertEqual(coach.payload['hint'],'下次建议：下蹲幅度偏小，保持躯干稳定，适当增加屈髋屈膝幅度')

    def test_current_cue_preserves_next_repetition_advice(self):
        coach=CompactCoach()
        coach.update(update(completed=True,cues=(cue(),)))
        coach.update(update(live_cues=(cue('tempo_seconds',CorrectionDirection.BELOW),)))
        self.assertEqual(coach.payload['hint'],'下次建议：下蹲幅度偏小，保持躯干稳定，适当增加屈髋屈膝幅度')

    def test_tracking_loss_preserves_next_repetition_advice(self):
        coach=CompactCoach()
        coach.update(update(completed=True,cues=(cue(),)))
        coach.update(update(valid=False))
        self.assertEqual(coach.payload['hint'],'下次建议：下蹲幅度偏小，保持躯干稳定，适当增加屈髋屈膝幅度')

    def test_invalid_completed_result_replaces_old_advice(self):
        coach=CompactCoach()
        coach.update(update(completed=True,cues=(cue(),)))
        coach.update(update(valid=False,completed=True))
        coach.update(update())
        self.assertEqual(coach.payload['hint'],'下次建议：本次有效观测不足，请确保双视角全身可见')

    def test_good_result_replaces_old_correction(self):
        coach=CompactCoach()
        coach.update(update(completed=True,cues=(cue(),)))
        coach.update(update(completed=True))
        self.assertEqual(coach.payload['hint'],'下次建议：本次动作符合当前评分要求，保持动作幅度与节奏')

    def test_directions_have_distinct_advice(self):
        for metric in ('side_depth_ratio','side_trunk_angle_deg','front_knee_symmetry','tempo_seconds'):
            messages=[]
            for direction in CorrectionDirection:
                coach=CompactCoach();coach.update(update(live_cues=(cue(metric,direction),)))
                messages.append(coach.payload['hint'])
            self.assertNotEqual(*messages)

    def test_unscored_summary_does_not_show_number(self):
        score=NS(valid=False,conformance_percent=95.)
        self.assertIn('上次得分  --',summary_text(1,score,{}))
        self.assertNotIn('95',summary_text(1,score,{}))

    def test_disabled_scoring_is_explicit(self):
        self.assertEqual(summary_text(0,None,{},enabled=False),'姿态预览 · 评分未开启')

    def test_phase_and_score(self):
        coach=CompactCoach();coach.update(update())
        self.assertIn('下蹲',summary_text(2,NS(valid=True,conformance_percent=86.),coach.payload))
        self.assertIn('86',summary_text(2,NS(valid=True,conformance_percent=86.),coach.payload))

    def test_advice_survives_geometry_process_serialization(self):
        import pickle
        from posepriv_coach.field_preview_geometry_process import _reply_wire, _validate_reply, _restore_reply
        from posepriv_coach.contracts import PoseFrame, CameraView
        request={'generation':1,'selected':(('front',1,100),('side',1,100))}
        work={'stages':{'front':{'score':0.},'side':{'score':0.}},
              'poses':{view.value:PoseFrame(view,1,100,{}) for view in CameraView}}
        coach=CompactCoach();coach.update(update(completed=True,cues=(cue(),)))
        prepared={'plan':NS(matrix_writes=tuple(NS(matrix=NS(values=(0,)*12)) for _ in range(16)),matrix_valid=(False,)*16),
                  'rig':NS(display_transform=(0.,)*16,reason='neutral',inferred_slots=()),
                  'observed_core':{'spine':False,'neck':False},'repetition_count':1,'completed_score':None,
                  'coach':coach.payload,'work':work,'score_started_ns':1,'scored_ns':2,'planned_ns':3}
        reply=pickle.loads(pickle.dumps(_reply_wire(prepared,request)))
        _validate_reply(reply,request)
        restored=_restore_reply(reply,work,())
        self.assertEqual(restored['coach']['hint'],'下次建议：下蹲幅度偏小，保持躯干稳定，适当增加屈髋屈膝幅度')
        self.assertEqual(restored['pair'].front.frame_id,1)

if __name__=='__main__':unittest.main()
