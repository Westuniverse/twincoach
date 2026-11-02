"""Consume actual PPS3 tile pixels in the comparison display (ADR 0081).

The caller owns transport lifetime, hardware identity checks and exclusive op4
texture ownership. This module never opens MMIO/devices, retries or draws a
software replacement after a failed hardware frame.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
from time import monotonic_ns

from .avatar_display import AvatarDisplayModel, ProjectedTriangle
from .projected_triangle_batch import ProjectedTriangleBatch
from .avatar_raster_packet import (
    RASTER_DMA_CAPACITY, TEXTURE, TILE, pack_texture_upload, unpack_response,
    reference_tile, response_header, unpack_command, validate_dma_request,
)
from .avatar_tile_plan import material_texture, prepare_tile_packets


@dataclass(frozen=True, slots=True)
class RasterFrame:
    width: int
    height: int
    pixels_rgbx8: bytes
    tiles: int
    commands: int
    texture_uploaded: bool
    preparation_ns: int
    exchange_ns: int
    assembly_ns: int


class ReferenceRasterPacketTransport:
    """Slow independent pixel-equation golden, for verification only; no device I/O."""
    def __init__(self):
        self._texture = None

    def exchange_raster_packet(self, payload):
        operation, _ = validate_dma_request(payload)
        if operation == TEXTURE:
            image = bytearray(256 * 512 * 3)
            for lane in range(3):
                image[lane::3] = payload[16 + lane::4]
            self._texture = bytes(image)
            return response_header(TEXTURE, 0)
        if self._texture is None:
            raise ValueError("reference texture must be uploaded first")
        header = int.from_bytes(payload[:16], "little")
        commands = [unpack_command(payload[i:i + 80]) for i in range(16, len(payload), 80)]
        return reference_tile(((header >> 64) & 4095, (header >> 76) & 4095),
                              (header >> 88) & 0xffffff, commands, self._texture)


class GoldenCheckingRasterTransport:
    """Instrumented verification wrapper, never a production FPS measurement."""
    def __init__(self, transport, *, clock_ns=monotonic_ns):
        self._transport = transport
        self._golden = ReferenceRasterPacketTransport()
        self._clock = clock_ns
        self.records = []

    def exchange_raster_packet(self, payload):
        expected = self._golden.exchange_raster_packet(payload)
        row = {"index": len(self.records), "input_bytes": len(payload), "byte_equal": False}
        self.records.append(row)
        start = self._clock()
        actual = self._transport.exchange_raster_packet(payload)
        row["actual_exchange_ns"] = self._clock() - start
        row["output_bytes"] = len(actual)
        if actual != expected:
            raise RuntimeError("actual raster response differs from entire independent byte golden")
        row["byte_equal"] = True
        return actual


class SharedRasterFrameRenderer:
    """Borrow one synchronous transport; return an entire immutable frame or fail."""

    def __init__(self, transport, *, clock_ns=monotonic_ns, packet_planner=None):
        if not callable(getattr(transport, "exchange_raster_packet", None)):
            raise TypeError("raster packet transport required")
        self._transport = transport
        if packet_planner is not None and not callable(packet_planner):
            raise TypeError("callable packet planner required")
        self._packet_planner = packet_planner if packet_planner is not None else prepare_tile_packets
        self._flat_row_planner = getattr(packet_planner, "plan_rows", None)
        self._clock = clock_ns
        self._texture = None
        self._faulted = False
        self._lock = threading.Lock()

    @property
    def supports_flat_rows(self):
        return callable(self._flat_row_planner)

    def render_flat_rows(self, rows, texture, *, width, height, background_rgb=0x0F1823):
        """Explicit private-array path; same planner ABI and atomic frame publication."""
        with self._lock:
            if self._faulted:
                raise RuntimeError("raster display faulted; explicit hardware recovery required")
            start = self._clock()
            if not self.supports_flat_rows:
                raise ValueError("flat rows require an explicit native row planner")
            if type(texture) is not bytes or len(texture) != 256 * 512 * 3:
                raise ValueError("complete immutable RGB8 material texture required")
            plan = self._flat_row_planner(rows, width, height, background_rgb)
            return self._finish_frame(plan, texture, width, height, start)

    def render(self, state, *, background_rgb=0x0F1823, material_colors=None) -> RasterFrame:
        with self._lock:
            if self._faulted:
                raise RuntimeError("raster display faulted; explicit hardware recovery required")
            start = self._clock()
            width, height = state.viewport_width, state.viewport_height
            if (type(width) is not int or type(height) is not int or
                    not 1 <= width <= 4096 or not 1 <= height <= 4096):
                raise ValueError("raster viewport must be 1..4096")
            if state.texture is not None:
                if (state.texture.width, state.texture.height) != (256, 512) or material_colors is not None:
                    raise ValueError("fixed texture or flat materials required, not both")
                texture, palette = state.texture.pixels_rgb8, None
            else:
                colors = material_colors if material_colors is not None else (
                    triangle.base_color_rgb for triangle in state.projected_triangles
                )
                colors = tuple(colors)
                texture, palette = material_texture(colors or ((0, 0, 0),))
            plan = self._packet_planner(state, palette=palette, background_rgb=background_rgb)
            return self._finish_frame(plan, texture, width, height, start)

    def _finish_frame(self, plan, texture, width, height, start):
        # Called under the frame lock, after the whole plan has succeeded.
        packets = plan.packets
        if any(len(packet) > RASTER_DMA_CAPACITY for _, packet in packets):
            raise ValueError("raster tile exceeds PPS3 DMA capacity")
        upload = pack_texture_upload(texture) if texture != self._texture else None
        prepared = self._clock()
        outputs = []
        try:
            if upload is not None:
                result = unpack_response(self._transport.exchange_raster_packet(upload))
                if result.op != TEXTURE or result.status or result.payload:
                    raise RuntimeError("failed or mismatched texture response")
            for origin, packet in packets:
                result = unpack_response(self._transport.exchange_raster_packet(packet))
                if result.op != TILE or result.status or len(result.payload) != 4096:
                    raise RuntimeError("failed or mismatched tile response")
                outputs.append((origin, result.payload))
        except BaseException:
            self._faulted = True
            self._texture = None
            raise
        exchanged = self._clock()
        image = bytearray(width * height * 4)
        for (x, y), payload in outputs:
            count = min(32, width - x) * 4
            for row in range(min(32, height - y)):
                offset = ((y + row) * width + x) * 4
                image[offset:offset + count] = payload[row * 128:row * 128 + count]
        pixels = bytes(image)
        assembled = self._clock()
        self._texture = texture
        return RasterFrame(width, height, pixels, len(packets), plan.command_count, upload is not None,
                           prepared - start, exchanged - prepared, assembled - exchanged)


class SharedRasterAvatarPainter:
    """Frame callback for current flat-material avatars; draws only inside its ROI.

    Projection rounding and final face colors are the same as the original UI.
    Raster edge coverage is the PPS3 pixel-centre rule, not OpenCV fillConvexPoly.
    """

    def __init__(self, renderer: SharedRasterFrameRenderer, *, numpy_module=None, flat_rows=False):
        if numpy_module is None:
            import numpy as numpy_module
        self._numpy = numpy_module
        self._renderer = renderer
        if flat_rows and not renderer.supports_flat_rows:
            raise ValueError("flat rows require an explicit native row planner")
        self._flat_rows = flat_rows
        self.supports_batch_snapshot = bool(flat_rows)
        self.last_frame: RasterFrame | None = None

    def __call__(self, canvas, rect, state) -> None:
        np = self._numpy
        self.last_frame = None
        if (not isinstance(canvas, np.ndarray) or canvas.dtype != np.uint8 or
                canvas.ndim != 3 or canvas.shape[2] != 3 or not canvas.flags.writeable):
            raise ValueError("writable BGR8 canvas required")
        if state.texture is not None:
            raise ValueError("display consumer currently requires flat materials")
        x, y, width, height = rect
        if (any(type(v) is not int for v in rect) or width < 3 or height < 3 or
                x < 0 or y < 0 or x + width > canvas.shape[1] or y + height > canvas.shape[0]):
            raise ValueError("complete panel rectangle must fit inside the canvas")
        if not state.projected_triangles:
            return
        if self._flat_rows:
            self._paint_flat_rows(canvas, rect, state)
            return
        scale = min(width / max(1, state.viewport_width), height / max(1, state.viewport_height))
        ox = x + round((width - state.viewport_width * scale) / 2)
        oy = y + round((height - state.viewport_height * scale) / 2)
        ordered = sorted(enumerate(state.projected_triangles), key=lambda item: item[1].depth, reverse=True)
        projected = []
        for index, triangle in ordered:
            vertices = tuple((ox + round(px * scale), oy + round(py * scale))
                             for px, py in triangle.vertices)
            if state.triangle_colors_bgr8:
                rgb = state.triangle_colors_bgr8[index][::-1]
            else:
                normalized = max(0., min(1., (triangle.depth + 1.) * .5))
                light = (1. - .30 * normalized) * max(.18, min(1., triangle.light_intensity))
                rgb = tuple(round(channel * light) for channel in triangle.base_color_rgb)
            projected.append((triangle, vertices, rgb))
        # Only the mesh bounding rectangle is sent, reducing empty-tile ioctls;
        # the existing caller already cleared the panel with this background.
        xs = [point[0] for _, vertices, _ in projected for point in vertices]
        ys = [point[1] for _, vertices, _ in projected for point in vertices]
        left, right = max(x + 1, min(xs)), min(x + width - 2, max(xs))
        top, bottom = max(y + 1, min(ys)), min(y + height - 2, max(ys))
        if left > right or top > bottom:
            return
        triangles = tuple(ProjectedTriangle(
            tuple((px - left, py - top) for px, py in vertices), triangle.depth, 1., rgb,
        ) for triangle, vertices, rgb in projected)
        local = replace(AvatarDisplayModel().snapshot(width=right-left+1, height=bottom-top+1),
                        projected_triangles=triangles)
        result = self._renderer.render(local, background_rgb=0x0F1823)
        # Conversion and canvas write occur after a complete successful frame.
        rgbx = np.frombuffer(result.pixels_rgbx8, np.uint8).reshape(result.height, result.width, 4)
        bgr = rgbx[:, :, (2, 1, 0)].copy()
        canvas[top:bottom + 1, left:right + 1] = bgr
        self.last_frame = result

    def _paint_flat_rows(self, canvas, rect, state):
        """Keep original face order/rounding without allocating another mesh of objects."""
        np = self._numpy
        x, y, width, height = rect
        scale = min(width / max(1, state.viewport_width), height / max(1, state.viewport_height))
        ox = x + round((width - state.viewport_width * scale) / 2)
        oy = y + round((height - state.viewport_height * scale) / 2)
        batch = state.projected_triangles
        if isinstance(batch, ProjectedTriangleBatch):
            faces = batch.array_view(np)
            order = np.argsort(-faces['depth'], kind='stable')
            count = len(order)
            pixels = faces['pixels'][order].astype(np.float64)
        else:
            ordered = sorted(enumerate(batch), key=lambda item: item[1].depth, reverse=True)
            order = [index for index, _ in ordered]
            count = len(ordered)
            pixels = np.fromiter((v for _, t in ordered for px, py in t.vertices for v in (px, py)),
                                 dtype=np.float64, count=count * 6).reshape(count, 3, 2)
        np.multiply(pixels, scale, out=pixels)
        np.rint(pixels, out=pixels)
        pixels[:, :, 0] += ox
        pixels[:, :, 1] += oy
        if not np.isfinite(pixels).all():
            raise ValueError("flat planner XY must be finite")
        left, right = max(x + 1, int(pixels[:, :, 0].min())), min(x + width - 2, int(pixels[:, :, 0].max()))
        top, bottom = max(y + 1, int(pixels[:, :, 1].min())), min(y + height - 2, int(pixels[:, :, 1].max()))
        if left > right or top > bottom:
            return
        pixels[:, :, 0] -= left
        pixels[:, :, 1] -= top
        if pixels.min() < -32768 or pixels.max() > 32767:
            raise ValueError("flat planner XY must be signed16 integers")
        if state.triangle_colors_bgr8:
            colors = tuple(state.triangle_colors_bgr8[index][::-1] for index in order)
        else:
            if isinstance(batch, ProjectedTriangleBatch):
                depths, lights = faces['depth'][order], faces['light'][order]
                rgb = faces['rgb'][order].astype(np.float64)
            else:
                factors = np.fromiter((v for _, t in ordered for v in (t.depth, t.light_intensity)),
                                      dtype=np.float64, count=count * 2).reshape(count, 2)
                depths, lights = factors[:, 0], factors[:, 1]
                rgb = np.fromiter((c for _, t in ordered for c in t.base_color_rgb),
                                  dtype=np.float64, count=count * 3).reshape(count, 3)
            normalized = np.clip((depths + 1.) * .5, 0., 1.)
            light = (1. - .30 * normalized) * np.clip(lights, .18, 1.)
            np.multiply(rgb, light[:, None], out=rgb)
            np.rint(rgb, out=rgb)
            colors = tuple(zip(*(map(int, rgb[:, lane]) for lane in range(3))))
        texture, palette = material_texture(colors)
        rows = np.empty((count, 9), dtype=np.int32)
        rows[:, :6] = pixels.reshape(count, 6)
        rows[:, 6:8] = np.fromiter((v for c in colors for v in palette[c]),
                                  dtype=np.int32, count=count * 2).reshape(count, 2)
        rows[:, 8] = 256
        result = self._renderer.render_flat_rows(rows, texture, width=right-left+1, height=bottom-top+1)
        rgbx = np.frombuffer(result.pixels_rgbx8, np.uint8).reshape(result.height, result.width, 4)
        canvas[top:bottom + 1, left:right + 1] = rgbx[:, :, (2, 1, 0)].copy()
        self.last_frame = result
