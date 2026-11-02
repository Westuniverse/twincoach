"""Display-only palm-down constraint; wrists remain measured pose joints."""
import math
from .skeleton_pose import BoneLink, stable_bone_local_x_axis

HAND_LINKS = (BoneLink('left_hand', 'left_wrist', 'left_hand_tip'),
              BoneLink('right_hand', 'right_wrist', 'right_hand_tip'))


def _dot(a,b): return sum(x*y for x,y in zip(a,b))


def _cross(a,b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _unit(a):
    length=math.sqrt(_dot(a,a))
    return tuple(x/length for x in a)


def _angle(a,b,direction):
    return math.atan2(_dot(direction,_cross(a,b)),_dot(a,b))


def _turn(axis,direction,angle):
    tangent=_cross(direction,axis)
    return _unit(tuple(math.cos(angle)*a+math.sin(angle)*b for a,b in zip(axis,tangent)))


def palm_down_x_axis(segment, *, previous_axis=None, max_roll_radians=math.pi):
    """Local +Z is palm normal. Upright fingers blend toward a relaxed inward palm."""
    if not segment.valid: return None
    direction=_unit(tuple(b-a for a,b in zip(segment.start,segment.end)))
    horizontal=math.hypot(direction[0],direction[2])
    weight=min(1.,horizontal/.5)
    inward=-1. if segment.name=='left_hand' else 1.
    if horizontal>1e-8:
        down=(0.,-1.,0.)
        palm=_unit(tuple(r-_dot(down,direction)*d for r,d in zip(down,direction)))
        desired=_unit(_cross(direction,palm))
    if weight<1.:
        relaxed=_unit(_cross(direction,(inward,0.,0.)))
        desired=(_turn(relaxed,direction,_angle(relaxed,desired,direction)*weight)
                 if horizontal>1e-8 else relaxed)
    # An observed extended arm must not retain a stale palm-up roll on reacquisition.
    if previous_axis is None or weight>=1.: return desired
    previous=stable_bone_local_x_axis(segment,previous_axis)
    angle=_angle(previous,desired,direction)
    angle=max(-max_roll_radians,min(max_roll_radians,angle))
    return _turn(previous,direction,angle)


def continuous_palm_down_x_axis(segment, *, previous_axis=None,
                                previous_direction=None, max_roll_radians=math.pi):
    """Transport real forearm swing, then bound only the remaining axial roll.

    A missing prior direction means first acquisition/reacquisition. Its palm
    follows the current target immediately, including an extended arm.
    """
    desired=palm_down_x_axis(segment,previous_axis=None)
    if desired is None or previous_axis is None or previous_direction is None:
        return desired
    direction=_unit(tuple(b-a for a,b in zip(segment.start,segment.end)))
    old_direction=_unit(previous_direction)
    swing=_cross(old_direction,direction)
    cosine=max(-1.,min(1.,_dot(old_direction,direction)))
    carried=previous_axis
    if cosine>-1.+1.e-8:
        first=_cross(swing,carried);second=_cross(swing,first)
        carried=tuple(v+a+b/(1.+cosine) for v,a,b in zip(carried,first,second))
    # Exactly opposite forearms use a half turn about the prior local X axis,
    # carrying that X unchanged. Projection removes only numerical residuals.
    carried=_unit(tuple(v-_dot(carried,direction)*d for v,d in zip(carried,direction)))
    angle=_angle(carried,desired,direction)
    return _turn(carried,direction,max(-max_roll_radians,min(max_roll_radians,angle)))
