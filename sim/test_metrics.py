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

    def test_invalid_window_and_clock(self):
        rows=[{'id':'a','source_ns':10,'done_ns':5}]
        for start,end in [(0,0),(2,1),(0,float('inf'))]:
            with self.subTest(window=(start,end)),self.assertRaises(ValueError):
                metrics.measure(rows,0,start,end)
        with self.assertRaisesRegex(ValueError,'clock domain'):
            metrics.measure(rows,0,0,1)

    def test_duplicate_before_window_is_not_new(self):
        rows=[{'id':('A','a'),'source_ns':0,'done_ns':100_000_000},
              {'id':('A','a'),'source_ns':0,'done_ns':1_100_000_000},
              {'id':('B','a'),'source_ns':1_000_000_000,'done_ns':1_200_000_000}]
        result=metrics.measure(rows,0,1,2)
        self.assertEqual(result['fresh_outputs'],1)
        self.assertEqual(result['p95_source_age_ms'],200)

if __name__=='__main__':unittest.main()
