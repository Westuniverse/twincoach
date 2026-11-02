#!/usr/bin/env python3
"""Project entry point for the standalone pipeline-metrics skill."""
import importlib.util
from pathlib import Path

path=Path(__file__).resolve().parent/'pipeline-metrics/scripts/measure.py'
spec=importlib.util.spec_from_file_location('pipeline_metrics',path)
engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
measure=engine.measure
if __name__=='__main__':engine.main()
