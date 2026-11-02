"""Pure scalar display-body reference/reacquisition motion, no observations."""
from dataclasses import dataclass
import math
import struct

IDENTITY_4=(1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.)
BASE_LIGHT=struct.unpack('<3f',struct.pack('<3f',-.35,.55,-.76))

@dataclass(frozen=True,slots=True)
class BodyFrame:
    pelvis: tuple[float,float,float]
    rotation: tuple[float,...]
    scale: float

@dataclass(frozen=True,slots=True)
class BodyMotion:
    frame: BodyFrame
    anchor_yaw: float
    inferred: bool
    catching_up: bool
    display_timestamp_ns: int


def _grid(values):return tuple(round(float(v)*16777216)/16777216 for v in values)
def _dot(a,b):return math.fsum(x*y for x,y in zip(a,b))
def _unit(a):
    length=math.sqrt(_dot(a,a))
    if length<1e-9:raise ValueError('degenerate body rotation')
    return tuple(v/length for v in a)

def _quaternion(r):
    a,b,c,d,e,f,g,h,i=r;trace=a+e+i
    if trace>0:
        s=2*math.sqrt(max(0.,1+trace));q=(s/4,(h-f)/s,(c-g)/s,(d-b)/s)
    else:
        axis=max(range(3),key=lambda k:(a,e,i)[k])
        if axis==0:
            s=2*math.sqrt(max(0.,1+a-e-i));q=((h-f)/s,s/4,(b+d)/s,(c+g)/s)
        elif axis==1:
            s=2*math.sqrt(max(0.,1+e-a-i));q=((c-g)/s,(b+d)/s,s/4,(f+h)/s)
        else:
            s=2*math.sqrt(max(0.,1+i-a-e));q=((d-b)/s,(c+g)/s,(f+h)/s,s/4)
    return _unit(q)

def _rotation(q):
    w,x,y,z=q
    return (1-2*(y*y+z*z),2*(x*y-w*z),2*(x*z+w*y),
            2*(x*y+w*z),1-2*(x*x+z*z),2*(y*z-w*x),
            2*(x*z-w*y),2*(y*z+w*x),1-2*(x*x+y*y))

def _frame_grid(frame):return BodyFrame(_grid(frame.pelvis),_grid(frame.rotation),_grid((frame.scale,))[0])

def display_affine(source,target):
    """Map the entire source pose into its display frame, row-major float32."""
    if source==target:return IDENTITY_4
    a,b=_frame_grid(source),_frame_grid(target);ratio=b.scale/a.scale
    linear=tuple(ratio*math.fsum(b.rotation[r*3+k]*a.rotation[c*3+k]for k in range(3))
                 for r in range(3)for c in range(3))
    translation=tuple(b.pelvis[r]-_dot(linear[r*3:r*3+3],a.pelvis)for r in range(3))
    values=tuple(v for r in range(3)for v in (*linear[r*3:r*3+3],translation[r]))+(0.,0.,0.,1.)
    return struct.unpack('<16f',struct.pack('<16f',*_grid(values)))

def compose_display_projection(base,display):
    """Four-term scalar reductions avoid architecture-specific float32 GEMM."""
    values=tuple(math.fsum(base[r*4+k]*display[k*4+c]for k in range(4))for r in range(4)for c in range(4))
    return struct.unpack('<16f',struct.pack('<16f',*values))

def display_light_direction(display,light=BASE_LIGHT):
    """Rotate the world light into the unchanged source-normal frame."""
    if display==IDENTITY_4:return light
    scale=math.sqrt(math.fsum(display[r*4]**2 for r in range(3)))
    values=tuple(math.fsum(display[r*4+c]*light[r]for r in range(3))/scale for c in range(3))
    return struct.unpack('<3f',struct.pack('<3f',*values))

def body_reference_frame(target,previous,*,display_timestamp_ns,bind_lateral=(1.,0.,0.)):
    """Return display frame and proposed immutable history; caller commits on success."""
    inferred=target is None
    if inferred:
        if previous is None:target=BodyFrame((0.,0.,0.),(1.,0.,0.,0.,1.,0.,0.,0.,1.),1.)
        else:
            yaw=previous.anchor_yaw;c=math.cos(yaw);s=math.sin(yaw)
            target=BodyFrame(previous.frame.pelvis,(c,0.,-s,0.,1.,0.,s,0.,c),previous.frame.scale)
    active=False;frame=target
    if previous is not None:
        elapsed=(display_timestamp_ns-previous.display_timestamp_ns)/1e9
        if elapsed<0:raise ValueError('non-monotonic body display timestamp')
        if (inferred or previous.inferred or previous.catching_up) and elapsed<=.25:
            old=previous.frame;wanted=_frame_grid(target)
            if elapsed==0:frame=old;active=previous.catching_up or inferred
            else:
                response=1-math.exp(-2*math.pi*6*elapsed)
                delta=tuple(b-a for a,b in zip(old.pelvis,wanted.pelvis));distance=math.sqrt(_dot(delta,delta))
                factor=min(response,2*elapsed/distance)if distance>1e-9 else 1.
                pelvis=tuple(a+factor*d for a,d in zip(old.pelvis,delta))
                change=math.log(wanted.scale/old.scale);step=max(-1.5*elapsed,min(1.5*elapsed,response*change))
                scale=old.scale*math.exp(step)
                qa=_quaternion(old.rotation);qb=_quaternion(wanted.rotation);cosine=_dot(qa,qb)
                if cosine<0:qb=tuple(-v for v in qb);cosine=-cosine
                cosine=max(-1.,min(1.,cosine));angle=2*math.acos(cosine)
                rotation=wanted.rotation;rotation_step=min(response*angle,4*elapsed)
                if angle>1e-5:
                    tangent=_unit(tuple(b-cosine*a for a,b in zip(qa,qb)))
                    q=_unit(tuple(math.cos(rotation_step/2)*a+math.sin(rotation_step/2)*b for a,b in zip(qa,tangent)))
                    rotation=_rotation(q)
                active=(distance*(1-factor)>.001 or abs(change-step)>.001 or angle-rotation_step>math.radians(.5))
                frame=_frame_grid(BodyFrame(pelvis,rotation,scale))
    cached=_frame_grid(frame)
    if inferred and previous is not None:yaw=previous.anchor_yaw
    else:
        r=cached.rotation;x=_dot(r[:3],bind_lateral);z=_dot(r[6:9],bind_lateral)
        yaw=math.atan2(z,x)if math.hypot(x,z)>1e-7 else(previous.anchor_yaw if previous else 0.)
        yaw=_grid((yaw,))[0]
    return frame,BodyMotion(cached,yaw,inferred,active,display_timestamp_ns)
