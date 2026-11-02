"""将三维骨骼帧转换成渲染器可直接消费的显示状态。

该模块只处理视图变换和显示统计，不参与三角测量、动作评分或蒙皮计算。
这样 A53/主机可以把同一份 ``AvatarFramePlan`` 交给参考后端、Verilator、
真实 DMA 和 HDMI 渲染器，而不会在显示层重复推理。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import math
from threading import RLock

from .avatar_image_source import ImageAvatarFramePlan, avatar_image_source
from .avatar_pipeline import AvatarFramePlan, AvatarFrameResult
from .avatar_skinning import PackedSkinningOutputBatch, SkinningOutputBeat
from .avatar_view import (
    OrbitCamera,
    PreparedOrbitProjection,
    ProjectedBoneSegment,
)
from .mesh_asset import AvatarMeshAsset
from .skeleton_pose import BoneSegment3D
from .contracts import PoseFrame
from .avatar_triangle_lighting_backend import LightingBatchStatistics
from .avatar_texture import AvatarTexture
from .projected_triangle_batch import ProjectedTriangleBatch, project_triangle_batch


@dataclass(frozen=True, slots=True)
class ProjectedTriangle:
    """一个已经投影到当前面板的网格三角形。"""

    vertices: tuple[tuple[int, int], ...]
    depth: float = 0.0
    light_intensity: float = 1.0
    base_color_rgb: tuple[int, int, int] = (116, 154, 38)
    uvs: tuple = ()

    def __post_init__(self) -> None:
        if self.uvs and (len(self.uvs) != 3 or any(len(uv) != 2 or
                any(not math.isfinite(v) or not 0 <= v <= 1 for v in uv) for uv in self.uvs)):
            raise ValueError("projected triangle UVs must be three pairs in 0..1")
        if len(self.vertices) != 3:
            raise ValueError("a projected triangle must contain three vertices")
        if not math.isfinite(self.depth):
            raise ValueError("projected triangle depth must be finite")
        if not math.isfinite(self.light_intensity) or not 0.0 <= self.light_intensity <= 1.0:
            raise ValueError("projected triangle light intensity must be within [0, 1]")
        if len(self.base_color_rgb) != 3:
            raise ValueError("projected triangle base color must be an RGB8 triple")
        red, green, blue = self.base_color_rgb
        # RGB has fixed arity: keep every public check and its short-circuit
        # order without allocating an iterator/generator for each visible face.
        if (
            not isinstance(red, int) or red < 0 or red > 255
            or not isinstance(green, int) or green < 0 or green > 255
            or not isinstance(blue, int) or blue < 0 or blue > 255
        ):
            raise ValueError("projected triangle base color must be an RGB8 triple")


@dataclass(frozen=True, slots=True)
class AvatarDisplayState:
    """一次三维数字人视图快照，坐标原点位于显示面板左上角。"""

    projected_segments: tuple[ProjectedBoneSegment, ...]
    viewport_width: int
    viewport_height: int
    valid_joint_count: int
    total_joint_count: int
    valid_bone_count: int
    total_bone_count: int
    confidence_percent: float
    skin_vertex_count: int
    frame_id: int | None
    yaw_degrees: float
    pitch_degrees: float
    zoom: float
    fit_scale: float
    projected_triangles: tuple[ProjectedTriangle, ...] | ProjectedTriangleBatch = ()
    valid_triangulated_point_count: int = 0
    total_triangulated_point_count: int = 0
    finite_ray_gap_count: int = 0
    mean_ray_gap_world_units: float | None = None
    mean_ray_angle_degrees: float | None = None
    triangle_colors_bgr8: tuple[tuple[int, int, int], ...] = ()
    lighting_statistics: LightingBatchStatistics | None = None
    pose_preview: bool = False
    texture: AvatarTexture | None = None

    def __post_init__(self) -> None:
        if type(self.triangle_colors_bgr8) is not tuple:
            raise ValueError("immutable BGR8 triangle colors required")
        if self.lighting_statistics is not None and (
            type(self.lighting_statistics) is not LightingBatchStatistics
            or self.lighting_statistics.face_count != len(self.projected_triangles)
            or len(self.triangle_colors_bgr8) != len(self.projected_triangles)
        ):
            raise ValueError("lighting statistics/colors must describe the complete snapshot")
        if self.triangle_colors_bgr8 and (
            len(self.triangle_colors_bgr8) != len(self.projected_triangles)
            or any(type(color) is not tuple or len(color) != 3
                   or any(type(v) is not int or not 0 <= v <= 255 for v in color)
                   for color in self.triangle_colors_bgr8)
        ):
            raise ValueError("complete native BGR8 triangle colors required")

    @property
    def observable_point_percent(self) -> float | None:
        if self.total_triangulated_point_count == 0:
            return None
        return 100.0 * self.finite_ray_gap_count / self.total_triangulated_point_count

    @property
    def accepted_geometry_percent(self) -> float | None:
        if self.finite_ray_gap_count == 0:
            return None
        return 100.0 * self.valid_triangulated_point_count / self.finite_ray_gap_count


@dataclass(frozen=True, slots=True)
class _FrozenOrbitCamera:
    yaw_degrees: float
    pitch_degrees: float
    zoom: float
    pan_x: float
    pan_y: float


@dataclass(frozen=True, slots=True)
class AvatarSnapshotRequest:
    """由 prepare_snapshot 捕获的显示输入；不引用可变 model/camera。

    解析不缓存画布或输出；排队但被替换的请求不需要执行投影。
    """

    viewport_width: int
    viewport_height: int
    camera: _FrozenOrbitCamera
    minimum_confidence: float
    _frame: AvatarFramePlan | ImageAvatarFramePlan | None
    _skin_vertex_count: int
    _skinned_points: tuple[tuple[float, float, float], ...]
    _mesh_asset: AvatarMeshAsset | None

    @property
    def frame_id(self) -> int | None:
        return None if self._frame is None else avatar_image_source(self._frame).frame_id

    def matches_pair(self, front: PoseFrame, side: PoseFrame) -> bool:
        if self._frame is None:
            return True  # An empty captured model has no pose geometry to mismatch.
        if getattr(self._frame, 'source', None) is not None:
            return False  # A learned image pose is never a scored stereo pair.
        pair = self._frame.pair
        return (
            pair.front.view is front.view and pair.side.view is side.view
            and pair.front.frame_id == front.frame_id
            and pair.side.frame_id == side.frame_id
            and pair.front.timestamp_ns == front.timestamp_ns
            and pair.side.timestamp_ns == side.timestamp_ns
            and pair.front.source_width == front.source_width
            and pair.front.source_height == front.source_height
            and pair.side.source_width == side.source_width
            and pair.side.source_height == side.source_height
        )

    def resolve(self) -> AvatarDisplayState:
        return _project_avatar_snapshot(
            self, width=self.viewport_width, height=self.viewport_height
        )

    def resolve_batch(self) -> AvatarDisplayState:
        """Explicit untextured immutable batch; original resolve stays unchanged."""
        return _project_avatar_snapshot(self, width=self.viewport_width,
                                        height=self.viewport_height, triangle_batch=True)

    def resolve_with_lighting(self, lighting_backend) -> AvatarDisplayState:
        """显式实验入口；使用捕获的几何，不取最新model、不重算合格面光照。"""
        if lighting_backend is None:
            raise ValueError("explicit lighting backend required")
        return _project_avatar_snapshot(self, width=self.viewport_width,
            height=self.viewport_height, lighting_backend=lighting_backend)


class AvatarDisplayModel:
    """保存最新数字人帧，并生成适合 OpenCV/framebuffer 的二维骨骼坐标。

    ``OrbitCamera`` 是交互状态：鼠标拖动、滚轮和复位操作只改变相机，不会
    修改三角测量结果或矩阵表。每次 ``snapshot`` 会按当前有效骨段重新居中，
    避免双摄世界坐标原点变化导致数字人跑出 HDMI 面板。
    """

    def __init__(
        self,
        camera: OrbitCamera | None = None,
        *,
        minimum_confidence: float = 0.0,
    ) -> None:
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be within [0, 1]")
        self.camera = camera if camera is not None else OrbitCamera()
        self.minimum_confidence = minimum_confidence
        self._lock = RLock()
        self._frame: AvatarFramePlan | ImageAvatarFramePlan | None = None
        self._skin_vertex_count = 0
        self._skinned_points: tuple[tuple[float, float, float], ...] = ()
        self._mesh_asset: AvatarMeshAsset | None = None

    def update(
        self,
        frame: AvatarFramePlan | ImageAvatarFramePlan | AvatarFrameResult,
        mesh_asset: AvatarMeshAsset | None = None,
    ) -> None:
        """接收一帧规划或已完成蒙皮的一帧结果。"""

        with self._lock:
            if isinstance(frame, AvatarFrameResult):
                self._frame = frame.plan
                self._skin_vertex_count = len(frame.output_beats)
                self._skinned_points = _decode_skinned_points_for_display(
                    frame.output_beats
                )
            elif isinstance(frame, (AvatarFramePlan, ImageAvatarFramePlan)):
                self._frame = frame
                self._skin_vertex_count = 0
                self._skinned_points = ()
            else:
                raise TypeError("frame must be an avatar render plan or AvatarFrameResult")
            self._mesh_asset = mesh_asset

    def clear(self) -> None:
        """清除当前数字人帧，但保留用户的相机角度和缩放。"""

        with self._lock:
            self._frame = None
            self._skin_vertex_count = 0
            self._skinned_points = ()
            self._mesh_asset = None

    def rotate(self, delta_yaw_degrees: float, delta_pitch_degrees: float) -> None:
        with self._lock:
            self.camera.rotate(delta_yaw_degrees, delta_pitch_degrees)

    def zoom_by(self, factor: float) -> None:
        with self._lock:
            self.camera.zoom_by(factor)

    def pan(self, delta_x: float, delta_y: float) -> None:
        with self._lock:
            self.camera.pan(delta_x, delta_y)

    def reset_camera(self) -> None:
        with self._lock:
            self.camera.reset()

    def snapshot(self, *, width: int, height: int) -> AvatarDisplayState:
        """按当前相机生成一个面板内的二维投影快照。"""

        with self._lock:
            return self._snapshot_unlocked(width=width, height=height)

    def snapshot_with_lighting(self, *, width: int, height: int, lighting_backend) -> AvatarDisplayState:
        """显式光照候选；默认snapshot及设备生命周期不变。"""
        return self.prepare_snapshot(width=width, height=height).resolve_with_lighting(lighting_backend)

    def prepare_snapshot(self, *, width: int, height: int) -> AvatarSnapshotRequest:
        """冻结当前帧及观察相机，但不计算投影/面剔除/光照。"""

        if width <= 0 or height <= 0:
            raise ValueError("avatar viewport dimensions must be positive")
        with self._lock:
            frame = self._frame
            if frame is not None:
                # MappingProxyType protects mappings, not coordinates held in
                # their values. Copy only small per-pose vectors, not the mesh.
                frame = replace(
                    frame,
                    joints={name: replace(joint, position=tuple(joint.position))
                            for name, joint in frame.joints.items()},
                    segments=tuple(replace(segment, start=tuple(segment.start),
                                           end=tuple(segment.end)) for segment in frame.segments),
                    model_origin=tuple(frame.model_origin),
                    # The projection does not need mutable backend requests.
                    matrix_writes=tuple(frame.matrix_writes),
                    matrix_valid=tuple(frame.matrix_valid),
                    vertex_requests=(),
                )
            camera = self.camera
            return AvatarSnapshotRequest(
                width, height,
                _FrozenOrbitCamera(camera.yaw_degrees, camera.pitch_degrees,
                                   camera.zoom, camera.pan_x, camera.pan_y),
                self.minimum_confidence, frame, self._skin_vertex_count,
                self._skinned_points, self._mesh_asset,
            )

    def _snapshot_unlocked(self, *, width: int, height: int, lighting_backend=None, triangle_batch=False) -> AvatarDisplayState:
        """锁内同步调用，或以已冻结请求作为只读输入调用。"""

        if width <= 0 or height <= 0:
            raise ValueError("avatar viewport dimensions must be positive")
        if triangle_batch and (lighting_backend is not None or
                               (self._mesh_asset is not None and self._mesh_asset.texture is not None)):
            raise ValueError('batch snapshot requires untextured CPU presentation lighting')
        frame = self._frame
        if frame is None:
            return AvatarDisplayState(
                projected_segments=(),
                viewport_width=width,
                viewport_height=height,
                valid_joint_count=0,
                total_joint_count=0,
                valid_bone_count=0,
                total_bone_count=0,
                confidence_percent=0.0,
                skin_vertex_count=0,
                frame_id=None,
                yaw_degrees=self.camera.yaw_degrees,
                pitch_degrees=self.camera.pitch_degrees,
                zoom=self.camera.zoom,
                fit_scale=1.0,
                projected_triangles=(),
            )

        valid_joints = tuple(
            joint
            for joint in frame.joints.values()
            if joint.valid and joint.confidence >= self.minimum_confidence
        )
        valid_segments = tuple(
            segment
            for segment in frame.segments
            if segment.valid and segment.confidence >= self.minimum_confidence
        )
        finite_ray_gaps = tuple(
            point.ray_gap
            for point in frame.points.values()
            if math.isfinite(point.ray_gap)
        )
        ray_angles = tuple(
            point.ray_angle_degrees
            for point in frame.points.values()
            if point.ray_angle_degrees > 0.0
        )
        center, fit_scale = _fit_transform(
            valid_segments,
            self._skinned_points,
            world_origin=frame.model_origin,
            world_scale=frame.model_scale,
        )
        if frame.pose_preview:
            # Fixed exercise framing must include a full-size rigid skull and feet.
            center, fit_scale = (0.0, 0.0, 0.0), 1.05

        projection = PreparedOrbitProjection.from_camera(
            self.camera, width=width, height=height
        )
        projected = tuple(
            ProjectedBoneSegment(
                name=segment.name,
                start=projection.project(
                    _normalise(
                        _world_to_model(
                            segment.start,
                            frame.model_origin,
                            frame.model_scale,
                        ),
                        center,
                        fit_scale,
                    ),
                )[:2],
                end=projection.project(
                    _normalise(
                        _world_to_model(
                            segment.end,
                            frame.model_origin,
                            frame.model_scale,
                        ),
                        center,
                        fit_scale,
                    ),
                )[:2],
            )
            for segment in valid_segments
        )
        camera_vertices, projected_vertices_with_depth = _project_skin_points(
            self._skinned_points,
            center=center,
            fit_scale=fit_scale,
            projection=projection,
        )
        projected_vertices = tuple(
            (projected[0], projected[1]) for projected in projected_vertices_with_depth
        )
        projected_triangles = ()
        triangle_colors_bgr8 = ()
        lighting_statistics = None
        if self._mesh_asset is not None:
            preview_vertex_valid = (
                tuple(frame.matrix_valid[request.matrix_indices[0]]
                      if request.weights_q0_15[0] == 32768 else
                      all(frame.matrix_valid[index] for index, weight in
                          zip(request.matrix_indices, request.weights_q0_15) if weight)
                      for request in self._mesh_asset.vertices)
                if frame.pose_preview else None
            )
            triangle_output: list[ProjectedTriangle] = []
            lighting_faces = [] if lighting_backend is not None else None
            lighting_uvs = []
            vertex_count = len(projected_vertices_with_depth)
            soft_lights=None
            if self._mesh_asset.soft_lighting:
                if lighting_backend is not None:
                    raise ValueError('soft presentation lighting cannot use the flat PL lighting backend')
                from .avatar_surface import soft_triangle_lights
                soft_lights=soft_triangle_lights(camera_vertices,self._mesh_asset.triangles,preview_vertex_valid)
            if triangle_batch:
                projected_triangles = project_triangle_batch(self._mesh_asset, camera_vertices,
                    projected_vertices_with_depth, preview_vertex_valid, soft_lights)
            else:
                for triangle_index, (first_index, second_index, third_index) in enumerate(
                    self._mesh_asset.triangles
                ):
                    if (
                        first_index >= vertex_count
                        or second_index >= vertex_count
                        or third_index >= vertex_count
                    ):
                        continue
                    if preview_vertex_valid is not None and not (
                        preview_vertex_valid[first_index] and preview_vertex_valid[second_index]
                        and preview_vertex_valid[third_index]
                    ):
                        continue
                    first = camera_vertices[first_index]
                    second = camera_vertices[second_index]
                    third = camera_vertices[third_index]
                    projected_points = (
                        projected_vertices[first_index],
                        projected_vertices[second_index],
                        projected_vertices[third_index],
                    )
                    double_sided = (
                        self._mesh_asset.triangle_double_sided[triangle_index]
                        if self._mesh_asset.triangle_double_sided
                        else True
                    )
                    if not double_sided and not _front_facing(projected_points):
                        continue
                    if lighting_faces is not None:
                        rgb = (self._mesh_asset.triangle_colors_rgb[triangle_index]
                               if self._mesh_asset.triangle_colors_rgb else (116, 154, 38))
                        lighting_faces.append((projected_points, (first, second, third), rgb))
                        lighting_uvs.append(self._mesh_asset.triangle_uvs[triangle_index]
                                            if self._mesh_asset.triangle_uvs else ())
                        continue  # Do not precompute CPU lighting before the PL transaction.
                    triangle_output.append(
                        ProjectedTriangle(
                            projected_points,
                            (first[2] + second[2] + third[2]) / 3.0,
                            float(soft_lights[triangle_index]) if soft_lights is not None else
                            _triangle_light_intensity((first, second, third)),
                            (
                                self._mesh_asset.triangle_colors_rgb[triangle_index]
                                if self._mesh_asset.triangle_colors_rgb
                                else (116, 154, 38)
                            ),
                            self._mesh_asset.triangle_uvs[triangle_index] if self._mesh_asset.triangle_uvs else (),
                        )
                    )
                if lighting_faces is not None:
                    shaded = lighting_backend.shade(tuple((points, rgb) for _, points, rgb in lighting_faces))
                    if (len(shaded.colors_bgr8) != len(lighting_faces)
                            or len(shaded.light_intensities) != len(lighting_faces)):
                        raise ValueError("lighting backend returned an incomplete frame")
                    triangle_output = [ProjectedTriangle(vertices,
                        (points[0][2] + points[1][2] + points[2][2]) / 3.0, light, rgb, uv)
                        for (vertices, points, rgb), light, uv in zip(lighting_faces, shaded.light_intensities, lighting_uvs)]
                    triangle_colors_bgr8 = shaded.colors_bgr8
                    lighting_statistics = shaded.statistics
                projected_triangles = tuple(triangle_output)
        confidence = (
            sum(joint.confidence for joint in valid_joints) / len(valid_joints) * 100.0
            if valid_joints
            else 0.0
        )
        return AvatarDisplayState(
            projected_segments=projected,
            viewport_width=width,
            viewport_height=height,
            valid_joint_count=len(valid_joints),
            total_joint_count=len(frame.joints),
            valid_bone_count=len(valid_segments),
            total_bone_count=len(frame.segments),
            confidence_percent=confidence,
            skin_vertex_count=self._skin_vertex_count,
            frame_id=avatar_image_source(frame).frame_id,
            yaw_degrees=self.camera.yaw_degrees,
            pitch_degrees=self.camera.pitch_degrees,
            zoom=self.camera.zoom,
            fit_scale=fit_scale,
            projected_triangles=projected_triangles,
            triangle_colors_bgr8=triangle_colors_bgr8,
            lighting_statistics=lighting_statistics,
            pose_preview=frame.pose_preview,
            texture=self._mesh_asset.texture if self._mesh_asset is not None else None,
            valid_triangulated_point_count=sum(
                point.valid for point in frame.points.values()
            ),
            total_triangulated_point_count=len(frame.points),
            finite_ray_gap_count=len(finite_ray_gaps),
            mean_ray_gap_world_units=(
                sum(finite_ray_gaps) / len(finite_ray_gaps)
                if finite_ray_gaps
                else None
            ),
            mean_ray_angle_degrees=(
                sum(ray_angles) / len(ray_angles) if ray_angles else None
            ),
        )


def _project_avatar_snapshot(
    source: AvatarDisplayModel | AvatarSnapshotRequest, *, width: int, height: int,
    lighting_backend=None, triangle_batch=False,
) -> AvatarDisplayState:
    """复用原投影方法；冻结请求提供同一只读字段，不取模型最新状态。"""

    if triangle_batch:
        return AvatarDisplayModel._snapshot_unlocked(source, width=width, height=height,
                                                    lighting_backend=lighting_backend, triangle_batch=True)
    if lighting_backend is None:
        return AvatarDisplayModel._snapshot_unlocked(source, width=width, height=height)
    return AvatarDisplayModel._snapshot_unlocked(source, width=width, height=height,
                                                lighting_backend=lighting_backend)


def _decode_skinned_points_for_display(
    output_beats: Sequence[SkinningOutputBeat],
) -> tuple[tuple[float, float, float], ...]:
    """Decode validated-width beats without temporary integer point objects."""

    if isinstance(output_beats, PackedSkinningOutputBatch):
        return output_beats.float_points()
    points: list[tuple[float, float, float]] = []
    q14 = 1 << 14
    for beat in output_beats:
        word = int.from_bytes(beat.data, "little")
        if word >> 48:
            raise ValueError("skinning output reserved bits must be zero")
        point_x = word & 0xFFFF
        point_y = (word >> 16) & 0xFFFF
        point_z = (word >> 32) & 0xFFFF
        if point_x & 0x8000:
            point_x -= 1 << 16
        if point_y & 0x8000:
            point_y -= 1 << 16
        if point_z & 0x8000:
            point_z -= 1 << 16
        points.append((point_x / q14, point_y / q14, point_z / q14))
    return tuple(points)


def _fit_transform(
    segments: tuple[BoneSegment3D, ...],
    extra_points: tuple[tuple[float, float, float], ...] = (),
    *,
    world_origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    world_scale: float = 1.0,
) -> tuple[tuple[float, float, float], float]:
    if not segments and not extra_points:
        return (0.0, 0.0, 0.0), 1.0
    points = tuple(
        _world_to_model(point, world_origin, world_scale)
        for segment in segments
        for point in (segment.start, segment.end)
    ) + extra_points
    minimum_x, minimum_y, minimum_z = points[0]
    maximum_x, maximum_y, maximum_z = points[0]
    for point_x, point_y, point_z in points[1:]:
        if point_x < minimum_x:
            minimum_x = point_x
        if point_y < minimum_y:
            minimum_y = point_y
        if point_z < minimum_z:
            minimum_z = point_z
        if point_x > maximum_x:
            maximum_x = point_x
        if point_y > maximum_y:
            maximum_y = point_y
        if point_z > maximum_z:
            maximum_z = point_z
    center = (
        (minimum_x + maximum_x) * 0.5,
        (minimum_y + maximum_y) * 0.5,
        (minimum_z + maximum_z) * 0.5,
    )
    # Preserve sum's x/y/z floating-point reduction (including Python versions
    # with compensated sum). sqrt is monotonic on
    # nonnegative distances, so reducing first gives the same radius with only
    # one sqrt and no generator per vertex. Initial values preserve input ties.
    center_x, center_y, center_z = center
    point_x, point_y, point_z = points[0]
    maximum_squared_radius = sum((
        (point_x - center_x) ** 2,
        (point_y - center_y) ** 2,
        (point_z - center_z) ** 2,
    ))
    for point_x, point_y, point_z in points[1:]:
        squared_radius = sum((
            (point_x - center_x) ** 2,
            (point_y - center_y) ** 2,
            (point_z - center_z) ** 2,
        ))
        if squared_radius > maximum_squared_radius:
            maximum_squared_radius = squared_radius
    radius = math.sqrt(maximum_squared_radius)
    # 0.82 leaves a small border at the default camera angle. The bounds keep
    # noisy, nearly coincident keypoints from producing an unusable scale.
    fit_scale = 0.82 / max(radius, 1.0e-6)
    return center, max(0.05, min(8.0, fit_scale))


def _normalise(
    point: tuple[float, float, float],
    center: tuple[float, float, float],
    scale: float,
) -> tuple[float, float, float]:
    return tuple(
        (value - center[index]) * scale for index, value in enumerate(point)
    )  # type: ignore[return-value]


def _world_to_model(
    point: tuple[float, float, float],
    origin: tuple[float, float, float],
    scale: float,
) -> tuple[float, float, float]:
    return tuple(
        (value - origin[index]) * scale for index, value in enumerate(point)
    )  # type: ignore[return-value]


def _project_skin_points(
    points: tuple[tuple[float, float, float], ...],
    *,
    center: tuple[float, float, float],
    fit_scale: float,
    projection: PreparedOrbitProjection,
) -> tuple[
    tuple[tuple[float, float, float], ...],
    tuple[tuple[int, int, float], ...],
]:
    """稠密顶点用已有可选 NumPy；低模/无依赖保留原标量路径。"""

    if len(points) < 256:
        return _project_skin_points_scalar(points, center=center, fit_scale=fit_scale, projection=projection)
    try:
        import numpy  # type: ignore[import-not-found]
    except ImportError:
        return _project_skin_points_scalar(points, center=center, fit_scale=fit_scale, projection=projection)
    # Keep separate float64 operations in the scalar expression order. Do not
    # use a matrix product/FMA or float32, which could change half-tie pixels.
    with numpy.errstate(over="ignore", invalid="ignore"):
        model = (numpy.asarray(points, dtype=numpy.float64) - numpy.asarray(center, dtype=numpy.float64)) * fit_scale
        model_x, model_y, model_z = model[:, 0], model[:, 1], model[:, 2]
        yaw_x = projection.cos_yaw * model_x - projection.sin_yaw * model_z
        yaw_z = projection.sin_yaw * model_x + projection.cos_yaw * model_z
        camera_y = projection.cos_pitch * model_y - projection.sin_pitch * yaw_z
        camera_z = projection.sin_pitch * model_y + projection.cos_pitch * yaw_z
        pixel_x = numpy.rint(projection.center_x + yaw_x * projection.scale + projection.pan_x)
        pixel_y = numpy.rint(projection.center_y - camera_y * projection.scale + projection.pan_y)
    depths = camera_z.tolist()
    camera_vertices = tuple(zip(yaw_x.tolist(), camera_y.tolist(), depths))
    # Python ints retain the original unlimited range; the renderer's int32
    # validation remains responsible for rejecting unrepresentable pixels.
    projected_vertices = tuple(zip(map(int, pixel_x.tolist()), map(int, pixel_y.tolist()), depths))
    return camera_vertices, projected_vertices


def _project_skin_points_scalar(
    points: tuple[tuple[float, float, float], ...],
    *,
    center: tuple[float, float, float],
    fit_scale: float,
    projection: PreparedOrbitProjection,
) -> tuple[
    tuple[tuple[float, float, float], ...],
    tuple[tuple[int, int, float], ...],
]:
    """批量投影蒙皮顶点，避免每个顶点重复创建中间 tuple 和方法调用。"""

    cos_yaw = projection.cos_yaw
    sin_yaw = projection.sin_yaw
    cos_pitch = projection.cos_pitch
    sin_pitch = projection.sin_pitch
    pixel_scale = projection.scale
    center_x = projection.center_x
    center_y = projection.center_y
    pan_x = projection.pan_x
    pan_y = projection.pan_y
    center_x_model, center_y_model, center_z_model = center
    camera_vertices: list[tuple[float, float, float]] = []
    projected_vertices: list[tuple[int, int, float]] = []
    for point_x, point_y, point_z in points:
        model_x = (point_x - center_x_model) * fit_scale
        model_y = (point_y - center_y_model) * fit_scale
        model_z = (point_z - center_z_model) * fit_scale
        yaw_x = cos_yaw * model_x - sin_yaw * model_z
        yaw_z = sin_yaw * model_x + cos_yaw * model_z
        camera_y = cos_pitch * model_y - sin_pitch * yaw_z
        camera_z = sin_pitch * model_y + cos_pitch * yaw_z
        camera_point = (yaw_x, camera_y, camera_z)
        camera_vertices.append(camera_point)
        projected_vertices.append(
            (
                round(center_x + yaw_x * pixel_scale + pan_x),
                round(center_y - camera_y * pixel_scale + pan_y),
                camera_z,
            )
        )
    return tuple(camera_vertices), tuple(projected_vertices)


def _triangle_light_intensity(
    points: tuple[tuple[float, float, float], ...],
) -> float:
    """用相机空间面法线计算双面漫反射，退化三角形只保留环境光。"""

    if len(points) != 3:
        raise ValueError("triangle lighting requires three points")
    # Keep coordinate indexing (including the existing malformed-point errors),
    # but do not repeat the outer point lookup for each edge coordinate.
    origin, first, second = points
    first_x = first[0] - origin[0]
    first_y = first[1] - origin[1]
    first_z = first[2] - origin[2]
    second_x = second[0] - origin[0]
    second_y = second[1] - origin[1]
    second_z = second[2] - origin[2]
    normal_x = first_y * second_z - first_z * second_y
    normal_y = first_z * second_x - first_x * second_z
    normal_z = first_x * second_y - first_y * second_x
    length = math.sqrt(normal_x * normal_x + normal_y * normal_y + normal_z * normal_z)
    if length <= 1.0e-12:
        return 0.35
    # 光源位于观察者左上方。取绝对值支持不同来源资产的正反绕序，
    # painter 排序仍负责可见面覆盖。
    diffuse = abs(
        (normal_x * -0.35 + normal_y * 0.55 + normal_z * -0.76) / length
    )
    intensity = 0.28 + 0.72 * diffuse
    # min(1.0, NaN) returns its first operand too; do not reverse this test.
    return intensity if intensity < 1.0 else 1.0


def _front_facing(points: tuple[tuple[int, int], ...]) -> bool:
    """glTF 正面为 CCW；屏幕 Y 轴向下，因此正面投影有负有向面积。"""

    if len(points) != 3:
        raise ValueError("triangle facing test requires three points")
    first, second, third = points
    signed_double_area = (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )
    return signed_double_area < 0


__all__ = ["AvatarDisplayModel", "AvatarDisplayState", "AvatarSnapshotRequest", "ProjectedTriangle"]
