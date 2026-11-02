#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Full33 board-local camera HDMI candidate; retains the existing capture owner."""
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent.parent/'gl'),str(HERE.parent.parent/'world')]
sys.path[:0]=[str(HERE),str(HERE.parent.parent/'actor'),str(HERE.parent.parent/'process'),str(HERE.parent.parent/'geometry')]
import posepriv_coach
posepriv_coach.__path__.insert(0,str(HERE))
from posepriv_coach.field_preview_full33_split import LivePreview


def main():
    from functools import partial
    from scripts.board import show_hdmi_camera
    # Retain the capture owner's SIGINT/SIGTERM, camera cleanup, report and
    # status/exit semantics; replace only the child-preview factory.
    show_hdmi_camera.run=partial(show_hdmi_camera.run,preview_factory=LivePreview)
    raise SystemExit(show_hdmi_camera.main())


if __name__=='__main__':main()
