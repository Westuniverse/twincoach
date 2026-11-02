"""PS triangle preparation for the standalone fixed-tile renderer; no device I/O."""
from dataclasses import dataclass, replace
from .avatar_raster import prepare_raster_command


@dataclass(frozen=True, slots=True)
class TilePacketPlan:
    packets: tuple[tuple[tuple[int, int], bytes], ...]
    command_count: int


def prepare_tile_packets(state, *, palette, background_rgb):
    """Original Python command construction and packing, without device I/O."""
    from .avatar_raster_packet import pack_tile
    tiles = plan_tiles(state, palette=palette)
    return TilePacketPlan(tuple((origin, pack_tile(origin, background_rgb, commands))
                                for origin, commands in tiles),
                          sum(len(commands) for _, commands in tiles))


def material_texture(colors):
    """Stable RGB material palette stored as exact texels, retained across frames."""
    colors=tuple(dict.fromkeys(tuple(color) for color in colors))
    if not colors or len(colors)>256*512 or any(len(c)!=3 or
            any(type(v) is not int or not 0<=v<=255 for v in c) for c in colors):
        raise ValueError('1..131072 RGB8 materials required')
    image=bytearray(256*512*3)
    for index,color in enumerate(colors):image[index*3:index*3+3]=bytes(color)
    return bytes(image),{color:(index%256,index//256) for index,color in enumerate(colors)}


def plan_tiles(state, *, palette=None):
    """Return all (origin, commands) in row order, preserving stable painter order.

    Edge tiles are padded to 32x32; consumers crop to the viewport. Texture mode uses
    the existing normalized UVs; untextured mode samples exact material texels.
    """
    width,height=state.viewport_width,state.viewport_height
    if not 1<=width<=4096 or not 1<=height<=4096:
        raise ValueError('viewport must fit within 4096x4096')
    columns=(width+31)//32;rows=(height+31)//32
    textured=state.texture is not None
    if textured:
        if (state.texture.width,state.texture.height)!=(256,512) or palette is not None:
            raise ValueError('256x512 texture required; palette is only for flat materials')
    elif palette is None:
        raise ValueError('material palette required for untextured mesh')
    buckets=[[] for _ in range(columns*rows)]
    for triangle in sorted(state.projected_triangles,key=lambda t:t.depth,reverse=True):
        min_x=max(0,min(p[0] for p in triangle.vertices));max_x=min(width-1,max(p[0] for p in triangle.vertices))
        min_y=max(0,min(p[1] for p in triangle.vertices));max_y=min(height-1,max(p[1] for p in triangle.vertices))
        if min_x>max_x or min_y>max_y:continue
        shade=round(triangle.light_intensity*256)
        if textured:
            if not triangle.uvs:raise ValueError('textured face has no UVs')
            uv=triangle.uvs
        else:
            px,py=palette[triangle.base_color_rgb];uv=((0.,0.),)*3
        for ty in range(min_y//32,max_y//32+1):
            for tx in range(min_x//32,max_x//32+1):
                command=prepare_raster_command(triangle.vertices,uv,(tx*32,ty*32,min(32,width-tx*32),min(32,height-ty*32)),shade)
                if command.empty:continue
                if not textured:command=replace(command,uv_seed=(px<<16,py<<16),uv_steps=(0,0,0,0))
                buckets[ty*columns+tx].append(command)
    return tuple(((index%columns*32,index//columns*32),tuple(commands))
                 for index,commands in enumerate(buckets))
