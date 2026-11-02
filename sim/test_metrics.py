import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('metrics',ROOT/'skill/metrics.py')
metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)

class MetricTests(unittest.TestCase):
    def test_duplicate_and_window_boundary(self):
        rows=[{'id':'a','source_ns':0,'done_ns':200_000_000},
              {'id':'a','source_ns':0,'done_ns':300_000_000},
              {'id':'b','source_ns':500_000_000,'done_ns':700_000_000},
              {'id':'c','source_ns':1_000_000_000,'done_ns':1_400_000_000},
              {'id':'d','source_ns':2_000_000_000,'done_ns':2_000_000_000}]
        result=metrics.measure(rows,0,0,2)
        self.assertEqual(result['fresh_outputs'],3)
        self.assertEqual(result['fresh_outputs_per_s'],1.5)
        self.assertEqual(result['p95_source_age_ms'],200)
        self.assertEqual(result['max_confirmation_gap_ms'],700)

if __name__=='__main__':unittest.main()
