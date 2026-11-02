"""预绑定数字人网格的轻量、可审计主机契约。

资产在进入运行时前转换成 Q1.14 顶点、四个矩阵索引和 Q0.15 权重。真实
GLB/glTF 导入器可以以后单独实现；板端和 RTL 不依赖文件格式，只消费这里
生成的固定顶点请求与三角形索引。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
from typing import Any, Mapping
from .avatar_texture import AvatarTexture

from .avatar_skinning import (
    AffineMatrixQ1_14,
    MATRIX_COUNT,
    SKIN_WEIGHT_SUM,
    VertexQ1_14,
    VertexSkinningRequest,
    quantize_skinning_weights,
)


@dataclass(frozen=True, slots=True)
class AvatarMeshVertex:
    """一个预绑定顶点，字段顺序与 AXI 蒙皮请求一致。"""

    position: VertexQ1_14
    matrix_indices: tuple[int, int, int, int]
    weights_q0_15: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.matrix_indices) != 4 or any(
            index < 0 or index >= MATRIX_COUNT for index in self.matrix_indices
        ):
            raise ValueError("mesh vertex matrix indices must fit the 16-entry bank")
        if len(self.weights_q0_15) != 4 or any(
            weight < 0 or weight > SKIN_WEIGHT_SUM for weight in self.weights_q0_15
        ):
            raise ValueError("mesh vertex weights must be unsigned Q0.15")
        if sum(self.weights_q0_15) != SKIN_WEIGHT_SUM:
            raise ValueError("mesh vertex weights must sum to 32768")

    def to_request(self) -> VertexSkinningRequest:
        return VertexSkinningRequest(
            self.position,
            self.matrix_indices,
            self.weights_q0_15,
        )


@dataclass(frozen=True, slots=True)
class AvatarMeshAsset:
    """预绑定网格及其最小来源/许可证元数据。"""

    name: str
    source: str
    license_id: str
    bone_names: tuple[str, ...]
    vertices: tuple[AvatarMeshVertex, ...]
    triangles: tuple[tuple[int, int, int], ...]
    bone_bind_lengths_q1_14: tuple[int, ...] = ()
    inverse_bind_matrices_q1_14: tuple[AffineMatrixQ1_14, ...] = ()
    triangle_colors_rgb: tuple[tuple[int, int, int], ...] = ()
    triangle_double_sided: tuple[bool, ...] = ()
    triangle_uvs: tuple = ()
    texture: AvatarTexture | None = None
    soft_lighting: bool = False
    _skinning_requests_cache: tuple[VertexSkinningRequest, ...] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.soft_lighting) is not bool:
            raise ValueError('soft_lighting must be boolean')
        if self.texture is not None:
            if type(self.texture) is not AvatarTexture or len(self.triangle_uvs) != len(self.triangles):
                raise ValueError("textured mesh requires UVs for every triangle")
            if any(len(face) != 3 or any(len(uv) != 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in uv)
                                        for uv in face) for face in self.triangle_uvs):
                raise ValueError("mesh UVs must be finite pairs in 0..1")
        elif self.triangle_uvs:
            raise ValueError("UVs require an avatar texture")
        object.__setattr__(self, "triangle_uvs", tuple(tuple(tuple(uv) for uv in face) for face in self.triangle_uvs))
        if not self.name or not self.source or not self.license_id:
            raise ValueError("mesh name, source and license_id must not be empty")
        if not self.bone_names or len(self.bone_names) > MATRIX_COUNT:
            raise ValueError("mesh asset must contain one to sixteen bones")
        if len(set(self.bone_names)) != len(self.bone_names) or any(
            not name for name in self.bone_names
        ):
            raise ValueError("mesh bone names must be unique and non-empty")
        if self.bone_bind_lengths_q1_14:
            if len(self.bone_bind_lengths_q1_14) != len(self.bone_names):
                raise ValueError("mesh bind lengths must match bone_names")
            if any(
                length <= 0 or length > (1 << 15) - 1
                for length in self.bone_bind_lengths_q1_14
            ):
                raise ValueError("mesh bind lengths must be positive signed Q1.14")
        if self.inverse_bind_matrices_q1_14 and (
            len(self.inverse_bind_matrices_q1_14) != len(self.bone_names)
        ):
            raise ValueError("mesh inverse bind matrices must match bone_names")
        if not self.vertices:
            raise ValueError("mesh asset must contain at least one vertex")
        if any(
            index >= len(self.bone_names)
            for vertex in self.vertices
            for index in vertex.matrix_indices
        ):
            raise ValueError("mesh vertex references an unnamed bone")
        for triangle in self.triangles:
            if len(triangle) != 3 or any(
                index < 0 or index >= len(self.vertices) for index in triangle
            ):
                raise ValueError("mesh triangle index is outside the vertex buffer")
        if self.triangle_colors_rgb:
            if len(self.triangle_colors_rgb) != len(self.triangles):
                raise ValueError("mesh triangle colors must match triangles")
            if any(
                len(color) != 3
                or any(not isinstance(channel, int) or channel < 0 or channel > 255 for channel in color)
                for color in self.triangle_colors_rgb
            ):
                raise ValueError("mesh triangle colors must be RGB8 triples")
        if self.triangle_double_sided:
            if len(self.triangle_double_sided) != len(self.triangles):
                raise ValueError("mesh triangle sidedness must match triangles")
            if any(not isinstance(value, bool) for value in self.triangle_double_sided):
                raise ValueError("mesh triangle sidedness entries must be boolean")
        object.__setattr__(self, "bone_names", tuple(self.bone_names))
        object.__setattr__(self, "vertices", tuple(self.vertices))
        # Display requests can share this frozen asset across threads. Freeze
        # inner sequences too; a tuple of caller-owned lists is not immutable.
        object.__setattr__(self, "triangles", tuple(tuple(face) for face in self.triangles))
        object.__setattr__(
            self,
            "bone_bind_lengths_q1_14",
            tuple(self.bone_bind_lengths_q1_14),
        )
        object.__setattr__(
            self,
            "inverse_bind_matrices_q1_14",
            tuple(self.inverse_bind_matrices_q1_14),
        )
        object.__setattr__(
            self, "triangle_colors_rgb", tuple(tuple(color) for color in self.triangle_colors_rgb)
        )
        object.__setattr__(
            self, "triangle_double_sided", tuple(self.triangle_double_sided)
        )

    @property
    def vertex_count(self) -> int:
        return len(self.vertices)

    @property
    def triangle_count(self) -> int:
        return len(self.triangles)

    def skinning_requests(self) -> tuple[VertexSkinningRequest, ...]:
        """Return the immutable vertex batch consumed by CPU/Verilator/AXI DMA."""

        cached = self._skinning_requests_cache
        if cached is None:
            cached = tuple(vertex.to_request() for vertex in self.vertices)
            object.__setattr__(self, "_skinning_requests_cache", cached)
        return cached

    def report_metadata(self) -> dict[str, object]:
        """返回不含顶点坐标的会话证据摘要。"""

        return {
            "name": self.name,
            "source": self.source,
            "license_id": self.license_id,
            "vertex_count": self.vertex_count,
            "triangle_count": self.triangle_count,
            "bone_count": len(self.bone_names),
            "bone_names": list(self.bone_names),
            "has_bind_lengths": bool(self.bone_bind_lengths_q1_14),
            "has_inverse_bind_matrices": bool(self.inverse_bind_matrices_q1_14),
            "colored_triangle_count": len(self.triangle_colors_rgb),
            "double_sided_triangle_count": sum(self.triangle_double_sided),
            "sidedness_recorded": bool(self.triangle_double_sided),
        }

    def to_mapping(self) -> dict[str, object]:
        """将固定点资产导出为可审计的 JSON 兼容映射。"""

        return {
            "name": self.name,
            "source": self.source,
            "license_id": self.license_id,
            "bone_names": list(self.bone_names),
            "bone_bind_lengths_q1_14": list(self.bone_bind_lengths_q1_14),
            "inverse_bind_matrices_q1_14": [
                list(matrix.values) for matrix in self.inverse_bind_matrices_q1_14
            ],
            "vertices": [
                {
                    "position": [
                        vertex.position.x,
                        vertex.position.y,
                        vertex.position.z,
                    ],
                    "matrix_indices": list(vertex.matrix_indices),
                    "weights_q0_15": list(vertex.weights_q0_15),
                }
                for vertex in self.vertices
            ],
            "triangles": [list(triangle) for triangle in self.triangles],
            "triangle_colors_rgb": [list(color) for color in self.triangle_colors_rgb],
            "triangle_double_sided": list(self.triangle_double_sided),
            **({"texture": self.texture.to_mapping(), "triangle_uvs": self.triangle_uvs}
               if self.texture is not None else {}),
            **({'soft_lighting':True} if self.soft_lighting else {}),
        }

    def bounds_q1_14(self) -> tuple[VertexQ1_14, VertexQ1_14]:
        """Return component-wise minimum and maximum bind-pose coordinates."""

        positions = tuple(vertex.position for vertex in self.vertices)
        return (
            VertexQ1_14(
                min(position.x for position in positions),
                min(position.y for position in positions),
                min(position.z for position in positions),
            ),
            VertexQ1_14(
                max(position.x for position in positions),
                max(position.y for position in positions),
                max(position.z for position in positions),
            ),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AvatarMeshAsset":
        """Decode the small JSON-compatible interchange mapping.

        The mapping deliberately stores fixed-point integers rather than floats;
        conversion from a source GLB is an offline asset-build step.
        """

        vertices = tuple(
            AvatarMeshVertex(
                position=VertexQ1_14(*_tuple_of_int(item["position"], 3, "position")),
                matrix_indices=_tuple_of_int(item["matrix_indices"], 4, "matrix_indices"),
                weights_q0_15=_tuple_of_int(item["weights_q0_15"], 4, "weights_q0_15"),
            )
            for item in _sequence_of_mappings(raw.get("vertices"), "vertices")
        )
        triangles = tuple(
            _tuple_of_int(triangle, 3, "triangle")
            for triangle in _sequence_of_sequences(raw.get("triangles"), "triangles")
        )
        return cls(
            name=_required_text(raw, "name"),
            source=_required_text(raw, "source"),
            license_id=_required_text(raw, "license_id"),
            bone_names=tuple(_sequence_of_text(raw.get("bone_names"), "bone_names")),
            vertices=vertices,
            triangles=triangles,
            bone_bind_lengths_q1_14=_sequence_of_ints(
                raw.get("bone_bind_lengths_q1_14", ()),
                "bone_bind_lengths_q1_14",
            ),
            inverse_bind_matrices_q1_14=tuple(
                AffineMatrixQ1_14(
                    _tuple_of_int(matrix, 12, "inverse_bind_matrix")
                )
                for matrix in _sequence_of_sequences(
                    raw.get("inverse_bind_matrices_q1_14", ()),
                    "inverse_bind_matrices_q1_14",
                )
            ),
            triangle_colors_rgb=tuple(
                _tuple_of_int(color, 3, "triangle_color")
                for color in _sequence_of_sequences(
                    raw.get("triangle_colors_rgb", ()),
                    "triangle_colors_rgb",
                )
            ),
            triangle_double_sided=_sequence_of_bools(
                raw.get("triangle_double_sided", ()),
                "triangle_double_sided",
            ),
            triangle_uvs=raw.get("triangle_uvs", ()),
            texture=AvatarTexture.from_mapping(raw["texture"]) if raw.get("texture") is not None else None,
            soft_lighting=raw.get('soft_lighting',False),
        )


def build_demo_mesh_asset() -> AvatarMeshAsset:
    """Build a tiny license-free box mesh for deterministic DMA/UI smoke tests."""

    q14 = 1 << 14
    positions = (
        (-q14 // 4, 0, -q14 // 6),
        (q14 // 4, 0, -q14 // 6),
        (q14 // 4, q14, -q14 // 6),
        (-q14 // 4, q14, -q14 // 6),
        (-q14 // 4, 0, q14 // 6),
        (q14 // 4, 0, q14 // 6),
        (q14 // 4, q14, q14 // 6),
        (-q14 // 4, q14, q14 // 6),
    )
    vertices = tuple(
        AvatarMeshVertex(
            VertexQ1_14(*position),
            (0, 0, 0, 0),
            (SKIN_WEIGHT_SUM, 0, 0, 0),
        )
        for position in positions[:4]
    ) + tuple(
        AvatarMeshVertex(
            VertexQ1_14(*position),
            (1, 1, 1, 1),
            (SKIN_WEIGHT_SUM, 0, 0, 0),
        )
        for position in positions[4:]
    )
    triangles = (
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (3, 2, 6),
        (3, 6, 7),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    )
    return AvatarMeshAsset(
        name="demo_box_avatar",
        source="project-generated smoke asset",
        license_id="PROJECT-GENERATED",
        bone_names=("spine", "neck"),
        vertices=vertices,
        triangles=triangles,
    )


def load_avatar_mesh_asset(path: str | Path) -> AvatarMeshAsset:
    """从 UTF-8 固定点 JSON 读取一个已登记来源的网格资产。"""

    asset_path = Path(path)
    try:
        raw = json.loads(asset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read avatar mesh asset: {asset_path}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("avatar mesh asset root must be an object")
    return AvatarMeshAsset.from_mapping(raw)


def build_demo_humanoid_mesh_asset() -> AvatarMeshAsset:
    """Build a license-free low-poly mannequin for the HDMI display probe.

    The asset intentionally uses separate boxes for each major bone. It is a
    visual/integration stand-in, not a claim about final human mesh quality.
    """

    bone_names = (
        "spine",
        "neck",
        "head",
        "left_upper_arm",
        "left_lower_arm",
        "right_upper_arm",
        "right_lower_arm",
        "left_upper_leg",
        "left_lower_leg",
        "right_upper_leg",
        "right_lower_leg",
    )
    vertices: list[AvatarMeshVertex] = []
    triangles: list[tuple[int, int, int]] = []
    bind_lengths: list[int] = []
    model_scale = 0.75

    def add_box(
        length: float,
        radius_x: float,
        radius_z: float,
        bone_index: int,
    ) -> None:
        base = len(vertices)
        bind_lengths.append(_round_q1_14(length * model_scale))
        corners = tuple(
            (sx * radius_x, (sy + 1.0) * length * 0.5, sz * radius_z)
            for sx, sy, sz in (
                (-1.0, -1.0, -1.0),
                (1.0, -1.0, -1.0),
                (1.0, 1.0, -1.0),
                (-1.0, 1.0, -1.0),
                (-1.0, -1.0, 1.0),
                (1.0, -1.0, 1.0),
                (1.0, 1.0, 1.0),
                (-1.0, 1.0, 1.0),
            )
        )
        vertices.extend(
            AvatarMeshVertex(
                quantize_mesh_position(corner, model_scale),
                (bone_index, bone_index, bone_index, bone_index),
                (SKIN_WEIGHT_SUM, 0, 0, 0),
            )
            for corner in corners
        )
        triangles.extend(
            (
                (base + 0, base + 1, base + 2),
                (base + 0, base + 2, base + 3),
                (base + 4, base + 6, base + 5),
                (base + 4, base + 7, base + 6),
                (base + 0, base + 4, base + 5),
                (base + 0, base + 5, base + 1),
                (base + 3, base + 2, base + 6),
                (base + 3, base + 6, base + 7),
                (base + 0, base + 3, base + 7),
                (base + 0, base + 7, base + 4),
                (base + 1, base + 5, base + 6),
                (base + 1, base + 6, base + 2),
            )
        )

    add_box(0.58, 0.28, 0.18, 0)
    add_box(0.28, 0.12, 0.12, 1)
    add_box(0.38, 0.20, 0.18, 2)
    add_box(0.52, 0.12, 0.12, 3)
    add_box(0.48, 0.10, 0.10, 4)
    add_box(0.52, 0.12, 0.12, 5)
    add_box(0.48, 0.10, 0.10, 6)
    add_box(0.66, 0.14, 0.14, 7)
    add_box(0.62, 0.11, 0.11, 8)
    add_box(0.66, 0.14, 0.14, 9)
    add_box(0.62, 0.11, 0.11, 10)
    return AvatarMeshAsset(
        name="demo_low_poly_humanoid",
        source="project-generated low-poly display smoke asset",
        license_id="PROJECT-GENERATED",
        bone_names=bone_names,
        vertices=tuple(vertices),
        triangles=tuple(triangles),
        bone_bind_lengths_q1_14=tuple(bind_lengths),
    )


def build_demo_detailed_humanoid_mesh_asset(
    *, resolution_multiplier: int = 1
) -> AvatarMeshAsset:
    """Build a denser procedural mannequin for the 3D display path.

    Limbs use tapered capsules; the head uses an ellipsoid in its local ``+Y`` frame. The
    result is still a project-generated smoke asset (no face or material
    textures; only a restrained display palette), but it exercises hundreds of
    skinned vertices and
    gives the HDMI preview a continuous body silhouette instead of eleven
    disconnected boxes.  A legal high-density GLB can later replace it
    without changing the fixed-point mesh contract.
    """

    if not isinstance(resolution_multiplier, int) or resolution_multiplier < 1:
        raise ValueError("resolution_multiplier must be a positive integer")

    bone_names = (
        "spine",
        "neck",
        "head",
        "left_upper_arm",
        "left_lower_arm",
        "right_upper_arm",
        "right_lower_arm",
        "left_upper_leg",
        "left_lower_leg",
        "right_upper_leg",
        "right_lower_leg",
    )
    vertices: list[AvatarMeshVertex] = []
    triangles: list[tuple[int, int, int]] = []
    triangle_colors: list[tuple[int, int, int]] = []
    bind_lengths: list[int] = []
    model_scale = 0.70

    # Material colors are display metadata only: they are not packed into the
    # 128-bit skinning transaction, so the existing PL/DMA ABI and 938-vertex
    # pressure point stay unchanged.  A restrained shirt/skin/trouser palette
    # gives the mannequin readable anatomy while a licensed GLB is pending.
    shirt = (54, 104, 148)
    skin = (214, 164, 132)
    trousers = (48, 65, 104)

    def add_capsule(
        bone_index: int,
        length: float,
        radius_x: float,
        radius_z: float,
        *,
        rings: int = 6,
        sides: int = 10,
        taper: float = 0.82,
        color_rgb: tuple[int, int, int],
    ) -> None:
        if length <= 0.0 or radius_x <= 0.0 or radius_z <= 0.0:
            raise ValueError("procedural bone dimensions must be positive")
        rings *= resolution_multiplier
        sides *= resolution_multiplier
        base = len(vertices)
        bind_lengths.append(_round_q1_14(length * model_scale))
        # The end rings are slightly smaller, which avoids a blocky silhouette
        # while keeping every vertex assigned to exactly one matrix.
        for ring in range(rings + 1):
            fraction = ring / rings
            if bone_index == 2:
                end_factor = 0.08 + 0.92 * math.sin(math.pi * fraction)
                y = length * (1.0 - math.cos(math.pi * fraction)) * 0.5
            else:
                end_factor = taper + (1.0 - taper) * math.sin(math.pi * fraction)
                y = length * fraction
            for side in range(sides):
                angle = 2.0 * math.pi * side / sides
                vertices.append(
                    AvatarMeshVertex(
                        quantize_mesh_position(
                            (
                                radius_x * end_factor * math.cos(angle),
                                y,
                                radius_z * end_factor * math.sin(angle),
                            ),
                            model_scale,
                        ),
                        (bone_index, bone_index, bone_index, bone_index),
                        (SKIN_WEIGHT_SUM, 0, 0, 0),
                    )
                )
        for ring in range(rings):
            for side in range(sides):
                next_side = (side + 1) % sides
                a = base + ring * sides + side
                b = base + ring * sides + next_side
                c = base + (ring + 1) * sides + next_side
                d = base + (ring + 1) * sides + side
                triangles.extend(((a, b, c), (a, c, d)))
                face_color = ((48, 42, 36) if bone_index == 2 and (ring + 0.5) / rings > 0.76
                              else color_rgb)
                triangle_colors.extend((face_color, face_color))
        # Add a small cap at each end so the silhouette remains closed when a
        # bone is viewed from its local axis.
        for ring_index, reverse in ((0, True), (rings, False)):
            center_index = len(vertices)
            vertices.append(
                AvatarMeshVertex(
                    quantize_mesh_position((0.0, length * ring_index / rings, 0.0), model_scale),
                    (bone_index, bone_index, bone_index, bone_index),
                    (SKIN_WEIGHT_SUM, 0, 0, 0),
                )
            )
            for side in range(sides):
                next_side = (side + 1) % sides
                first = base + ring_index * sides + side
                second = base + ring_index * sides + next_side
                triangles.append(
                    (center_index, second, first) if reverse else (center_index, first, second)
                )
                triangle_colors.append((48, 42, 36) if bone_index == 2 and ring_index == rings else color_rgb)

    # Canonical local lengths are deliberately modest so all vertices remain
    # within signed Q1.14 after the common model scale.
    add_capsule(0, 0.58, 0.30, 0.20, rings=7, sides=12, taper=0.92, color_rgb=shirt)
    add_capsule(1, 0.28, 0.08, 0.08, rings=5, sides=10, taper=0.86, color_rgb=skin)
    add_capsule(2, 0.38, 0.15 if resolution_multiplier > 1 else 0.22,
                0.14 if resolution_multiplier > 1 else 0.20,
                rings=7, sides=12, taper=0.90, color_rgb=skin)
    add_capsule(3, 0.52, 0.13, 0.13, rings=6, sides=10, taper=0.82, color_rgb=skin)
    add_capsule(4, 0.48, 0.105, 0.105, rings=6, sides=10, taper=0.78, color_rgb=skin)
    add_capsule(5, 0.52, 0.13, 0.13, rings=6, sides=10, taper=0.82, color_rgb=skin)
    add_capsule(6, 0.48, 0.105, 0.105, rings=6, sides=10, taper=0.78, color_rgb=skin)
    add_capsule(7, 0.66, 0.18, 0.16, rings=7, sides=12, taper=0.90, color_rgb=trousers)
    add_capsule(8, 0.62, 0.135, 0.13, rings=7, sides=12, taper=0.82, color_rgb=trousers)
    add_capsule(9, 0.66, 0.18, 0.16, rings=7, sides=12, taper=0.90, color_rgb=trousers)
    add_capsule(10, 0.62, 0.135, 0.13, rings=7, sides=12, taper=0.82, color_rgb=trousers)
    high_detail = resolution_multiplier > 1
    return AvatarMeshAsset(
        name="demo_high_detail_humanoid" if high_detail else "demo_detailed_humanoid",
        source=(
            "project-generated capsule mannequin with rounded head and display palette"
            if not high_detail
            else "project-generated high-detail capsule stress asset; not a final licensed GLB"
        ),
        license_id="PROJECT-GENERATED",
        bone_names=bone_names,
        vertices=tuple(vertices),
        triangles=tuple(triangles),
        bone_bind_lengths_q1_14=tuple(bind_lengths),
        triangle_colors_rgb=tuple(triangle_colors),
        # The generated mannequin is an opaque surface. Recording it as
        # single-sided lets the HDMI painter discard back-facing triangles
        # before sorting/rasterisation; a future GLB can still override this
        # per material through the normal asset importer.
        triangle_double_sided=tuple(False for _ in triangles),
    )


def build_demo_high_detail_humanoid_mesh_asset() -> AvatarMeshAsset:
    """Build the project-generated L2 stress asset using the same packet ABI."""

    return build_demo_detailed_humanoid_mesh_asset(resolution_multiplier=2)


def build_coach_humanoid_mesh_asset(appearance=None) -> AvatarMeshAsset:
    """A closed, clothed presentation model on the existing eleven-bone ABI."""
    base = build_demo_detailed_humanoid_mesh_asset()
    vertices = []
    for vertex in base.vertices:
        bone = vertex.matrix_indices[0]
        x, y, z = (value / 16384 for value in
                   (vertex.position.x, vertex.position.y, vertex.position.z))
        if bone == 0:
            x *= .65 + .15 * y / (base.bone_bind_lengths_q1_14[0] / 16384)
            z *= .85
        elif bone == 2:
            x *= .70
            z *= .65
        if appearance is not None:
            if bone == 0:
                fraction = y / (base.bone_bind_lengths_q1_14[0] / 16384)
                x *= appearance.waist_scale * (1-fraction) + appearance.shoulder_scale * fraction
            elif bone == 2:
                x *= appearance.head_scale
                z *= appearance.head_scale
            elif bone in (3,4,5,6):
                x *= appearance.arm_scale
                z *= appearance.arm_scale
            elif bone in (7,8,9,10):
                x *= appearance.leg_scale
                z *= appearance.leg_scale
        vertices.append(replace(vertex, position=quantize_mesh_position((x, y, z), 1.0)))
    triangles = list(base.triangles)
    colors = list(base.triangle_colors_rgb)
    sided = list(base.triangle_double_sided)
    shirt, skin, trousers, shoes = (54, 104, 148), (214, 164, 132), (48, 65, 104), (35, 43, 55)
    if appearance is not None:
        shirt, skin, trousers, shoes = appearance.shirt_rgb, appearance.skin_rgb, appearance.trousers_rgb, appearance.shoes_rgb
    for i, triangle in enumerate(triangles):
        bone = vertices[triangle[0]].matrix_indices[0]
        if bone in (3, 5) and sum(vertices[v].position.y for v in triangle) / 3 < base.bone_bind_lengths_q1_14[bone] * .55:
            colors[i] = shirt
        if appearance is not None:
            colors[i] = (shirt if bone == 0 or bone in (3,5) or (bone in (4,6) and appearance.long_sleeves)
                         else trousers if bone in (7,8,9,10) else skin)

    def ellipsoid(bone, center, radii, color, *, rings=5, sides=10):
        first = len(vertices)
        for ring in range(rings + 1):
            latitude = math.pi * ring / rings
            for side in range(sides):
                angle = 2 * math.pi * side / sides
                point = (center[0] + radii[0] * math.sin(latitude) * math.cos(angle),
                         center[1] - radii[1] * math.cos(latitude),
                         center[2] + radii[2] * math.sin(latitude) * math.sin(angle))
                if appearance is not None:
                    scale = appearance.arm_scale if bone in (3,4,5,6) else max(.65,appearance.leg_scale) if bone in (7,8,9,10) else appearance.head_scale if bone == 2 else 1.
                    point = (point[0]*scale,point[1],point[2]*scale)
                vertices.append(AvatarMeshVertex(quantize_mesh_position(point, 1.0),
                                                (bone, bone, bone, bone), (32768, 0, 0, 0)))
        for ring in range(rings):
            for side in range(sides):
                a = first + ring * sides + side
                b = first + ring * sides + (side + 1) % sides
                c, d = b + sides, a + sides
                faces = []
                if ring > 0:
                    faces.append((a, b, c))
                if ring < rings - 1:
                    faces.append((a, c, d))
                triangles.extend(faces)
                colors.extend(color for _ in faces)
                sided.extend(True for _ in faces)

    for bone in (3, 5):
        ellipsoid(bone, (0, 0, 0), (.07, .055, .07), shirt)
    for bone in (4, 6):
        length = base.bone_bind_lengths_q1_14[bone] / 16384
        ellipsoid(bone, (0, 0, 0), (.061, .045, .061), skin)
        ellipsoid(bone, (0, length + .035, 0), (.047, .070, .028), skin)
    for bone in (8, 10):
        length = base.bone_bind_lengths_q1_14[bone] / 16384
        ellipsoid(bone, (0, 0, 0), (.085, .055, .075), trousers)
        ellipsoid(bone, (0, length - .010, .050), (.075, .045, .125), shoes)
    head_length = base.bone_bind_lengths_q1_14[2] / 16384
    for x in (-.036, .036):
        ellipsoid(2, (x, head_length * .61, -.087), (.010, .012, .007), (32, 34, 38), rings=3, sides=8)
    ellipsoid(2, (0, head_length * .51, -.10), (.013, .017, .017), skin, rings=3, sides=8)
    ellipsoid(2, (0, head_length * .33, -.09), (.026, .006, .005), (160, 95, 80), rings=3, sides=8)
    return replace(base, name="coach_personalized" if appearance is not None else "coach_humanoid", source="project-generated humanoid; appearance palette and proportions only" if appearance is not None else "project-generated clothed humanoid with joint contours, face, palms and shoes",
                   vertices=tuple(vertices), triangles=tuple(triangles), triangle_colors_rgb=tuple(colors),
                   triangle_double_sided=tuple(sided),
                   license_id="PROJECT-GENERATED+USER-PROVIDED-PRIVATE" if appearance is not None else base.license_id)


def quantize_mesh_position(position: tuple[float, float, float], scale: float) -> VertexQ1_14:
    """Convert model-space metres to signed Q1.14 without silent saturation."""

    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("mesh scale must be finite and positive")
    if len(position) != 3 or not all(math.isfinite(value) for value in position):
        raise ValueError("mesh position must contain three finite values")
    values = tuple(_round_q1_14(value * scale) for value in position)
    return VertexQ1_14(*values)


def quantize_mesh_weights(weights: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Normalize floating bind weights with the same deterministic rounding as RTL input."""

    return quantize_skinning_weights(weights)


def _round_q1_14(value: float) -> int:
    scaled = value * (1 << 14)
    rounded = math.floor(scaled + 0.5) if scaled >= 0.0 else math.ceil(scaled - 0.5)
    if rounded < -(1 << 15) or rounded > (1 << 15) - 1:
        raise ValueError("mesh position exceeds signed Q1.14 range")
    return rounded


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"mesh mapping requires non-empty {key}")
    return value


def _sequence_of_mappings(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"mesh mapping requires a sequence for {name}")
    if any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"mesh {name} entries must be mappings")
    return tuple(value)


def _sequence_of_sequences(value: Any, name: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"mesh mapping requires a sequence for {name}")
    return tuple(value)


def _sequence_of_text(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"mesh mapping requires text sequence for {name}")
    return tuple(value)


def _sequence_of_ints(value: Any, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in value
    ):
        raise ValueError(f"mesh mapping requires integer sequence for {name}")
    return tuple(value)


def _sequence_of_bools(value: Any, name: str) -> tuple[bool, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, bool) for item in value
    ):
        raise ValueError(f"mesh mapping requires boolean sequence for {name}")
    return tuple(value)


def _tuple_of_int(value: Any, length: int, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"mesh {name} must contain exactly {length} integers")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise ValueError(f"mesh {name} must contain exactly {length} integers")
    return tuple(value)


__all__ = [
    "AvatarMeshAsset",
    "AvatarMeshVertex",
    "build_demo_mesh_asset",
    "build_demo_humanoid_mesh_asset",
    "build_demo_detailed_humanoid_mesh_asset",
    "build_demo_high_detail_humanoid_mesh_asset",
    "load_avatar_mesh_asset",
    "quantize_mesh_position",
    "quantize_mesh_weights",
]
