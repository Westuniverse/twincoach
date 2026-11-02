"""Standalone PXT1/PXR1 wire ABI for the textured tile candidate (ADR 0072)."""
from dataclasses import dataclass
from .avatar_raster import RasterCommand, compose_tile, raster_fragments, shade_fragment

REQUEST_MAGIC=0x31545850
RESPONSE_MAGIC=0x31525850
TEXTURE=1
TILE=2


def pack_command(command: RasterCommand) -> bytes:
    if type(command) is not RasterCommand: raise ValueError('RasterCommand required')
    fields=[(value,12) for value in command.bbox]
    fields += [(edge[field],32) for field in range(3) for edge in command.edges]
    fields += [(value,40) for value in command.uv_seed]
    fields += [(value,32) for value in command.uv_steps]
    fields += [(command.shade,9),(int(command.empty),1)]
    value=offset=0
    for field,width in fields:
        value |= (field&((1<<width)-1))<<offset
        offset+=width
    return value.to_bytes(80,'little')


def unpack_command(data: bytes) -> RasterCommand:
    if len(data)!=80: raise ValueError('command is five 128-bit beats')
    value=int.from_bytes(data,'little')
    if value>>554: raise ValueError('reserved command bits must be zero')
    offset=0
    def take(width,signed=False):
        nonlocal offset
        field=(value>>offset)&((1<<width)-1);offset+=width
        return field-(1<<width) if signed and field&(1<<(width-1)) else field
    bbox=tuple(take(12) for _ in range(4))
    lanes=[tuple(take(32,True) for _ in range(3)) for _ in range(3)]
    edges=tuple(tuple(lanes[j][i] for j in range(3)) for i in range(3))
    uv_seed=tuple(take(40,True) for _ in range(2));uv_steps=tuple(take(32,True) for _ in range(4))
    return RasterCommand(bbox,edges,uv_seed,uv_steps,take(9),bool(take(1)))


def pack_texture_upload(texture_rgb8: bytes) -> bytes:
    if len(texture_rgb8)!=256*512*3: raise ValueError('256x512 RGB8 texture required')
    result=bytearray(16+256*512*4)
    result[:16]=(REQUEST_MAGIC|TEXTURE<<32|1<<40).to_bytes(16,'little')
    for lane in range(3):result[16+lane::4]=texture_rgb8[lane::3]
    return bytes(result)


def pack_tile(origin,background_rgb,commands) -> bytes:
    commands=tuple(commands)
    if (len(origin)!=2 or any(type(v) is not int or not 0<=v<=4064 for v in origin)
            or type(background_rgb) is not int or not 0<=background_rgb<1<<24 or len(commands)>65535):
        raise ValueError('invalid tile packet fields')
    header=REQUEST_MAGIC|TILE<<32|1<<40|len(commands)<<48|origin[0]<<64|origin[1]<<76|background_rgb<<88
    return header.to_bytes(16,'little')+b''.join(pack_command(command) for command in commands)


def response_header(op: int,status: int,payload_beats: int=0) -> bytes:
    if op not in (TEXTURE,TILE) or status not in (0,1,2) or payload_beats not in (0,256):
        raise ValueError('invalid response fields')
    return (RESPONSE_MAGIC|status<<32|1<<40|op<<48|payload_beats<<64).to_bytes(16,'little')


@dataclass(frozen=True,slots=True)
class RasterResponse:
    op: int
    status: int
    payload: bytes


def unpack_response(data: bytes) -> RasterResponse:
    if len(data)<16 or len(data)%16: raise ValueError('response must contain whole beats')
    header=int.from_bytes(data[:16],'little')
    op=(header>>48)&255;status=(header>>32)&255;count=(header>>64)&65535
    if (header&0xffffffff!=RESPONSE_MAGIC or (header>>40)&255!=1 or header>>80 or (header>>56)&255
            or op not in (TEXTURE,TILE) or status not in (0,1,2) or count not in (0,256)
            or len(data)!=16+count*16 or (status==1 and count) or (op==TEXTURE and (status==2 or count))
            or (op==TILE and status!=1 and count!=256)):
        raise ValueError('invalid response header/length')
    if any(data[19::4]): raise ValueError('RGBX padding must be zero')
    return RasterResponse(op,status,data[16:])


def reference_tile(origin,background_rgb,commands,texture_rgb8) -> bytes:
    def fragments():
        for command in commands:
            for x,y,u,v in raster_fragments(command):
                rgb=shade_fragment(texture_rgb8,u,v,command.shade)
                yield y<<16|x,int.from_bytes(bytes(rgb),'little'),False,False
            yield 0,0,True,False
    data,error=compose_tile(origin,background_rgb,fragments())
    return response_header(TILE,2 if error else 0,256)+data


RASTER_DMA_CAPACITY=(1<<20)-16


def validate_dma_request(payload: bytes) -> tuple[int,int]:
    """PPS3 DMA preflight, returning wire op and expected response bytes.

    Independent fixed wire ABI permits more commands than this DMA's capacity.
    Never split a long request by adding TLAST. Kernel repeats these checks.
    """
    if type(payload) is not bytes or not 16<=len(payload)<=RASTER_DMA_CAPACITY or len(payload)%16:
        raise ValueError('PPS3 request requires complete beats within DMA capacity')
    h=int.from_bytes(payload[:16],'little');op=(h>>32)&255;count=(h>>48)&65535
    x=(h>>64)&4095;y=(h>>76)&4095
    if h&0xffffffff!=REQUEST_MAGIC or (h>>40)&255!=1 or h>>112:
        raise ValueError('invalid PXT1 header')
    if op==TEXTURE:
        if len(payload)!=524304 or h>>48 or payload[19::4].strip(b'\0'):
            raise ValueError('invalid fixed texture request or RGBX padding')
        return op,16
    if op!=TILE or x>4064 or y>4064 or len(payload)!=16+80*count:
        raise ValueError('invalid tile request header/length')
    for offset in range(16,len(payload),80):
        command=unpack_command(payload[offset:offset+80]);x0,x1,y0,y1=command.bbox
        if not command.empty and x1>=x0 and y1>=y0 and not (x<=x0<=x1<x+32 and y<=y0<=y1<y+32):
            raise ValueError('triangle bbox exceeds selected tile')
    return op,4112
