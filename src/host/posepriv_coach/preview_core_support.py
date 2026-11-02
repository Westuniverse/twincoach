"""Current cross-view confidence support for display-only front torso points."""
from dataclasses import dataclass
import math

CORE_NAMES=('left_shoulder','right_shoulder','left_hip','right_hip')
CORE_SUPPORT_STATUS_ZH='部分点位较弱 · 动作显示参考'


@dataclass(frozen=True)
class CoreViewSupport:
    names: tuple[str,...]
    reason: str
    source_age_ns: int


def _visible(point,threshold):
    return (point is not None and point.confidence>=threshold and
            math.isfinite(point.x) and math.isfinite(point.y) and
            0<=point.x<=1 and 0<=point.y<=1)


def current_core_support(pair,trusted_names,*,forward_sign,display_timestamp_ns):
    ready=max(pair.front.timestamp_ns,pair.side.timestamp_ns)
    if type(display_timestamp_ns) is not int or display_timestamp_ns<ready:
        raise ValueError('current core support requires an integer display time at or after both sources')
    age=display_timestamp_ns-ready
    def reject(reason):return CoreViewSupport((),reason,age)
    missing=tuple(name for name in CORE_NAMES if name not in trusted_names)
    if not missing:return reject('not_needed')
    if age>100_000_000:return reject('stale_source')
    if forward_sign not in (-1.,1.):return reject('unknown_forward')
    front=pair.front;side=pair.side
    if not all(_visible(front.keypoints.get(name),.30) for name in CORE_NAMES):
        return reject('front_confidence')
    if not all(_visible(side.keypoints.get(name),.50) for name in set(missing)|{'left_hip','right_hip'}):
        return reject('side_confidence')
    scale=max(front.source_width,front.source_height)
    xy={name:(front.keypoints[name].x*front.source_width/scale,
              front.keypoints[name].y*front.source_height/scale) for name in CORE_NAMES}
    shoulder=math.dist(xy['left_shoulder'],xy['right_shoulder'])
    hip=math.dist(xy['left_hip'],xy['right_hip'])
    center=lambda a,b:tuple((x+y)/2 for x,y in zip(xy[a],xy[b]))
    torso=math.dist(center('left_shoulder','right_shoulder'),center('left_hip','right_hip'))
    if shoulder<.02 or not .25<=hip/shoulder<=1.5 or not .15<=torso/shoulder<=4.:
        return reject('front_geometry')
    return CoreViewSupport(missing,'supported',age)
