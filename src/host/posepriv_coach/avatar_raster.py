"""Fixed-point reference for the standalone PL raster/texture pipeline v1."""
from dataclasses import dataclass
import math
from .avatar_bilinear import bilinear_rgb8


@dataclass(frozen=True, slots=True)
class RasterCommand:
    bbox: tuple[int, int, int, int]  # x0,x1,y0,y1 inclusive
    edges: tuple[tuple[int,int,int], ...]  # seed,dx,dy at first pixel centre
    uv_seed: tuple[int,int]  # signed Q16 texel coordinates
    uv_steps: tuple[int,int,int,int]  # du_dx,dv_dx,du_dy,dv_dy
    shade: int=256
    empty: bool=False

    def __post_init__(self):
        if (len(self.bbox)!=4 or any(type(v) is not int or not 0<=v<4096 for v in self.bbox)
                or len(self.edges)!=3 or any(len(e)!=3 for e in self.edges)
                or len(self.uv_seed)!=2 or len(self.uv_steps)!=4
                or type(self.shade) is not int or not 0<=self.shade<=256 or type(self.empty) is not bool):
            raise ValueError('invalid raster command fields')
        x0,x1,y0,y1=self.bbox
        if x1-x0>31 or y1-y0>31: raise ValueError('raster patch exceeds 32 pixels')
        for edge in self.edges:
            for value,limit in zip(edge,(1<<30,1<<20,1<<20)):
                if type(value) is not int or not -limit<=value<limit: raise ValueError('edge exceeds range')
        for values,limit in ((self.uv_seed,1<<36),(self.uv_steps,1<<28)):
            if any(type(v) is not int or not -limit<=v<limit for v in values):
                raise ValueError('UV exceeds range')


def prepare_raster_command(vertices, uvs, tile, shade=256):
    """Integer screen vertices, normalized UVs, x/y/width/height patch <=32."""
    if (len(vertices)!=3 or len(uvs)!=3
            or any(len(p)!=2 or any(type(v) is not int or not -32768<=v<=32767 for v in p) for p in vertices)
            or any(len(p)!=2 or any(not math.isfinite(v) or not 0<=v<=1 for v in p) for p in uvs)):
        raise ValueError('three signed16 XY vertices and normalized UV pairs required')
    if len(tile)!=4 or any(type(v) is not int for v in tile): raise ValueError('invalid tile')
    tx,ty,w,h=tile
    if not 0<=tx<4096 or not 0<=ty<4096 or not 1<=w<=32 or not 1<=h<=32:
        raise ValueError('invalid tile')
    x0=max(tx,min(p[0] for p in vertices));x1=min(4095,tx+w-1,max(p[0] for p in vertices))
    y0=max(ty,min(p[1] for p in vertices));y1=min(4095,ty+h-1,max(p[1] for p in vertices))
    ax,ay=vertices[0];bx,by=vertices[1];cx,cy=vertices[2]
    area=(by-cy)*(ax-cx)+(cx-bx)*(ay-cy)
    if area==0 or x0>x1 or y0>y1:
        return RasterCommand((0,0,0,0),((0,0,0),)*3,(0,0),(0,0,0,0),shade,True)
    sign=1 if area>0 else -1
    edges=[]
    for j,k in ((1,2),(2,0),(0,1)):
        xj,yj=vertices[j];xk,yk=vertices[k]
        dx=sign*2*(yj-yk);dy=sign*2*(xk-xj)
        seed=sign*((yj-yk)*(2*x0+1-2*xk)+(xk-xj)*(2*y0+1-2*yk))
        edges.append((seed,dx,dy))
    # Round each Q16 plane coefficient once, ties-to-even; PL only adds integers.
    def plane(axis,edge_field):
        return round(sum(e[edge_field]*uv[axis] for e,uv in zip(edges,uvs))/(2*abs(area))
                     *(255 if axis==0 else 511)*65536)
    return RasterCommand((x0,x1,y0,y1),tuple(edges),(plane(0,0),plane(1,0)),
                         (plane(0,1),plane(1,1),plane(0,2),plane(1,2)),shade)


def raster_fragments(command):
    """Direct equations, intentionally independent of the RTL's running sums."""
    if command.empty: return
    x0,x1,y0,y1=command.bbox
    for y in range(y0,y1+1):
        for x in range(x0,x1+1):
            if all(seed+(x-x0)*dx+(y-y0)*dy>=0 for seed,dx,dy in command.edges):
                u=command.uv_seed[0]+(x-x0)*command.uv_steps[0]+(y-y0)*command.uv_steps[2]
                v=command.uv_seed[1]+(x-x0)*command.uv_steps[1]+(y-y0)*command.uv_steps[3]
                yield x,y,max(0,min(255<<16,u))>>8,max(0,min(511<<16,v))>>8


def shade_fragment(texture_rgb8, u_q8, v_q8, shade=256):
    if len(texture_rgb8)!=256*512*3: raise ValueError('256x512 RGB8 texture required')
    x,y=u_q8>>8,v_q8>>8
    if not 0<=x<=255 or not 0<=y<=511: raise ValueError('UV outside texture')
    def rgb(px,py):
        offset=(py*256+px)*3
        return tuple(texture_rgb8[offset:offset+3])
    return bilinear_rgb8((rgb(x,y),rgb(min(x+1,255),y),rgb(x,min(y+1,511)),
                         rgb(min(x+1,255),min(y+1,511))),u_q8&255,v_q8&255,shade)[0]


def compose_tile(origin, background_rgb, fragments):
    """v1 32x32 RGBX tile; fragments are (tag, packed RGB, last, error).

    Painter order, no depth/blending. Return packed row-major bytes and sticky fault.
    Markers never paint. Invalid global origins produce a background/error tile.
    """
    if (len(origin)!=2 or any(type(v) is not int or not 0<=v<=4095 for v in origin)
            or type(background_rgb) is not int or not 0<=background_rgb<1<<24):
        raise ValueError('12-bit origin and RGB24 background required')
    ox,oy=origin
    fault=ox>4064 or oy>4064
    pixels=[[background_rgb for _ in range(32)] for _ in range(32)]
    for tag,rgb,last,error in fragments:
        if type(tag) is not int or not 0<=tag<1<<32 or type(rgb) is not int or not 0<=rgb<1<<24:
            raise ValueError('invalid fragment payload')
        fault=bool(fault or error)
        if last: continue
        x,y=tag&65535,tag>>16
        if ox>4064 or oy>4064 or not ox<=x<ox+32 or not oy<=y<oy+32:
            fault=True
        elif not error:
            pixels[y-oy][x-ox]=rgb
    return b''.join(rgb.to_bytes(4,'little') for row in pixels for rgb in row),fault
