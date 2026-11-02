import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/host'))
from posepriv_coach.exercises.squat_tracker import SquatTracker,SquatPhaseConfig,SquatPhase

class SquatTests(unittest.TestCase):
    def test_complete_repetition_counts_once(self):
        tracker=SquatTracker()
        updates=[tracker.update(angle,i*200_000_000) for i,angle in enumerate([170,145,100,125,170,170])]
        self.assertEqual([u.phase for u in updates[:5]],[SquatPhase.STANDING,SquatPhase.DESCENDING,
            SquatPhase.BOTTOM,SquatPhase.ASCENDING,SquatPhase.STANDING])
        self.assertEqual(sum(u.repetition_completed for u in updates),1)
        self.assertEqual(updates[-1].repetition_count,1)
        self.assertAlmostEqual(updates[4].repetition_duration_seconds,.6)

    def test_gap_cannot_join_two_half_repetitions(self):
        tracker=SquatTracker()
        for angle,stamp in [(170,0),(145,200_000_000),(100,400_000_000),
                            (125,1_500_000_000),(170,1_700_000_000)]:
            result=tracker.update(angle,stamp)
        self.assertEqual(result.repetition_count,0)
        self.assertEqual(result.phase,SquatPhase.STANDING)

    def test_missing_frames_do_not_extend_hold_deadline(self):
        tracker=SquatTracker(SquatPhaseConfig(missing_tracking_policy='hold'))
        for angle,stamp in [(170,0),(145,200_000_000),(100,400_000_000)]:tracker.update(angle,stamp)
        result=tracker.update(None,800_000_000)
        self.assertEqual(result.phase,SquatPhase.UNKNOWN)
        self.assertEqual(tracker.phase,SquatPhase.BOTTOM)
        tracker.update(None,1_300_000_000)
        tracker.update(None,1_500_000_000)
        self.assertEqual(tracker.phase,SquatPhase.UNKNOWN)
        tracker.update(125,1_600_000_000)
        self.assertEqual(tracker.update(170,1_800_000_000).repetition_count,0)

if __name__=='__main__':unittest.main()
