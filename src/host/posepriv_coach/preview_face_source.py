"""Immutable association for display-only face axes from one completed build.

The side sign belongs to the preview planner, not a new camera calibration.
Project GPL-2.0-only; no model, image, dependency or observation is created here.
"""
from dataclasses import dataclass

from .contracts import CameraView


def pair_source_key(pair):
    frames=(pair.front,pair.side)
    if any(frame.view is not view for frame,view in zip(frames,(CameraView.FRONT,CameraView.SIDE))):
        raise ValueError('face source requires the original logical view order')
    key=tuple((f.frame_id,f.timestamp_ns,f.source_width,f.source_height)for f in frames)
    if any(type(v)is not int for row in key for v in row)or any(
            row[0]<0 or row[1]<0 or row[2]<=0 or row[3]<=0 for row in key):
        raise ValueError('face source identity requires integer IDs, clocks and dimensions')
    return key


@dataclass(frozen=True,slots=True)
class FaceAxisSource:
    key: tuple
    planner_timestamp_ns: int
    side_forward_sign: float | None

    def __post_init__(self):
        if (type(self.key)is not tuple or len(self.key)!=2 or
                any(type(row)is not tuple or len(row)!=4 for row in self.key)or
                any(type(v)is not int for row in self.key for v in row)or
                any(row[0]<0 or row[1]<0 or row[2]<=0 or row[3]<=0 for row in self.key)):
            raise ValueError('invalid face source identity')
        if (type(self.planner_timestamp_ns)is not int or
                self.planner_timestamp_ns!=(self.key[0][1]+self.key[1][1])//2):
            raise ValueError('face source must match the original pair clock')
        if self.side_forward_sign is not None and (
                type(self.side_forward_sign)is not float or self.side_forward_sign not in(-1.,1.)):
            raise ValueError('face source requires a captured preview sign')

    def matches(self,pair):
        return self.key==pair_source_key(pair)
