"""使用标准库读取 glTF/GLB 蒙皮网格并转换为固定点头像资产。

转换器只处理项目需要的静态数据边界：三角形 POSITION、一个 skin 的
JOINTS_0/WEIGHTS_0、可选索引和材质基础色。纹理、动画和 Draco 压缩留给显示层；
这样导入阶段不会把不适合 KV260 PL 数据面的内容偷偷带进运行时。
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import struct
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .avatar_skinning import AffineMatrixQ1_14, MATRIX_COUNT
from .mesh_asset import AvatarMeshAsset, AvatarMeshVertex, quantize_mesh_position, quantize_mesh_weights


class GltfAssetError(ValueError):
    """报告不支持或不完整的 glTF/GLB 资产。"""


_COMPONENTS: dict[int, tuple[str, int, bool]] = {
    5121: ("B", 1, False),
    5123: ("H", 2, False),
    5125: ("I", 4, False),
    5126: ("f", 4, True),
}
_TYPE_COUNTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def load_gltf_mesh_asset(
    path: str | Path,
    *,
    name: str | None = None,
    source: str | None = None,
    license_id: str | None = None,
    target_peak: float = 1.5,
    max_vertices: int = 20_000,
    max_triangles: int = 40_000,
    bone_name_map: Mapping[str, str] | None = None,
    bone_representatives: Mapping[str, str] | None = None,
) -> AvatarMeshAsset:
    """读取一个 glTF/GLB 网格并生成 ``AvatarMeshAsset``。

    ``license_id`` 必须由队员显式填写；转换器不会根据文件名猜许可证。
    坐标按最大绝对坐标缩放到 ``target_peak``，但不静默裁剪。当前 PL
    矩阵表为 16 项，因此 skin 的关节数超过 16 时直接报错。
    """

    asset_path = Path(path)
    if not asset_path.is_file():
        raise GltfAssetError(f"找不到 glTF/GLB 文件：{asset_path}")
    if not isinstance(target_peak, (int, float)) or not math.isfinite(target_peak) or target_peak <= 0.0:
        raise GltfAssetError("target_peak 必须是有限正数")
    if max_vertices < 1 or max_triangles < 1:
        raise GltfAssetError("网格容量上限必须为正数")
    if not license_id:
        raise GltfAssetError("必须显式提供 license_id，不能从文件名推断许可证")

    document, buffers = _read_document(asset_path)
    asset_header = document.get("asset")
    if not isinstance(asset_header, Mapping) or str(asset_header.get("version")) != "2.0":
        raise GltfAssetError("只支持 glTF 2.0 资产")
    meshes = document.get("meshes")
    if not isinstance(meshes, list) or not meshes:
        raise GltfAssetError("glTF 必须至少包含一个 mesh")
    for mesh_index, mesh in enumerate(meshes):
        if not isinstance(mesh, Mapping) or not isinstance(mesh.get("primitives"), list):
            raise GltfAssetError(f"mesh[{mesh_index}] 缺少 primitives")
    _validate_mesh_nodes(document, len(meshes))

    source_bone_names, source_inverse_bind = _read_skin(document, buffers)
    bone_names, source_to_palette, inverse_bind_source = _build_bone_palette(
        source_bone_names,
        source_inverse_bind,
        bone_name_map,
        bone_representatives,
    )
    if len(bone_names) > MATRIX_COUNT:
        raise GltfAssetError(
            f"skin 包含 {len(bone_names)} 根骨骼，当前 PL 矩阵表最多 {MATRIX_COUNT} 根"
        )
    bindings: list[tuple[tuple[float, float, float], tuple[int, int, int, int], tuple[int, int, int, int]]] = []
    triangles: list[tuple[int, int, int]] = []
    triangle_colors: list[tuple[int, int, int]] = []
    triangle_double_sided: list[bool] = []
    has_explicit_material = False
    positions_for_scale: list[tuple[float, float, float]] = []

    for mesh_index, primitive_index, primitive in _iter_mesh_primitives(meshes):
        if not isinstance(primitive, Mapping):
            raise GltfAssetError(
                f"mesh[{mesh_index}].primitive[{primitive_index}] 不是对象"
            )
        mode = int(primitive.get("mode", 4))
        if mode != 4:
            raise GltfAssetError(
                f"primitive[{primitive_index}] 使用 mode={mode}，当前只支持 TRIANGLES(4)"
            )
        attributes = primitive.get("attributes")
        if not isinstance(attributes, Mapping) or "POSITION" not in attributes:
            raise GltfAssetError(f"primitive[{primitive_index}] 缺少 POSITION")
        extra_joint_sets = sorted(
            name
            for name in attributes
            if str(name).startswith(("JOINTS_", "WEIGHTS_"))
            and name not in ("JOINTS_0", "WEIGHTS_0")
        )
        if extra_joint_sets:
            raise GltfAssetError(
                "当前 PL 顶点 ABI 每个顶点只支持四个骨骼影响；"
                "请在导出前把额外权重限制并归一化到 JOINTS_0/WEIGHTS_0："
                + ", ".join(str(name) for name in extra_joint_sets)
            )
        has_joints = "JOINTS_0" in attributes
        has_weights = "WEIGHTS_0" in attributes
        if has_joints != has_weights:
            raise GltfAssetError("JOINTS_0 与 WEIGHTS_0 必须成对出现")
        positions = _read_accessor(document, buffers, attributes["POSITION"], "POSITION")
        if any(len(value) != 3 for value in positions):
            raise GltfAssetError("POSITION 必须是 VEC3")
        if len(bindings) + len(positions) > max_vertices:
            raise GltfAssetError(f"顶点数超过上限 {max_vertices}，请先离线减面")
        joints = _read_optional_accessor(
            document, buffers, attributes.get("JOINTS_0"), "JOINTS_0"
        )
        weights = _read_optional_accessor(
            document, buffers, attributes.get("WEIGHTS_0"), "WEIGHTS_0"
        )
        if joints is not None and len(joints) != len(positions):
            raise GltfAssetError("JOINTS_0 与 POSITION 顶点数不一致")
        if weights is not None and len(weights) != len(positions):
            raise GltfAssetError("WEIGHTS_0 与 POSITION 顶点数不一致")
        if joints is None:
            joints = [(0, 0, 0, 0)] * len(positions)
        if weights is None:
            weights = [(1.0, 0.0, 0.0, 0.0)] * len(positions)
        base = len(bindings)
        for position, joint_values, weight_values in zip(positions, joints, weights, strict=True):
            if len(joint_values) != 4 or len(weight_values) != 4:
                raise GltfAssetError("JOINTS_0/WEIGHTS_0 必须是 VEC4")
            source_indices = tuple(int(value) for value in joint_values)
            if any(
                index < 0 or index >= max(1, len(source_bone_names))
                for index in source_indices
            ):
                raise GltfAssetError("顶点引用了 skin 之外的关节索引")
            indices, merged_weights = _merge_vertex_influences(
                source_indices,
                tuple(float(value) for value in weight_values),
                source_to_palette,
            )
            position_tuple = (float(position[0]), float(position[1]), float(position[2]))
            weight_tuple = quantize_mesh_weights(merged_weights)
            bindings.append((position_tuple, indices, weight_tuple))
            positions_for_scale.append(position_tuple)

        primitive_indices = _read_indices(document, buffers, primitive.get("indices"), len(positions))
        if len(primitive_indices) % 3 != 0:
            raise GltfAssetError("三角形索引数量必须是 3 的倍数")
        if len(triangles) + len(primitive_indices) // 3 > max_triangles:
            raise GltfAssetError(f"三角形数超过上限 {max_triangles}，请先离线减面")
        primitive_color = _read_primitive_base_color(document, primitive, primitive_index)
        primitive_double_sided = _read_primitive_double_sided(
            document, primitive, primitive_index
        )
        if primitive_color is not None:
            has_explicit_material = True
        triangle_count = len(primitive_indices) // 3
        triangle_colors.extend((primitive_color or (255, 255, 255),) * triangle_count)
        triangle_double_sided.extend((primitive_double_sided,) * triangle_count)
        triangles.extend(
            (base + primitive_indices[offset], base + primitive_indices[offset + 1], base + primitive_indices[offset + 2])
            for offset in range(0, len(primitive_indices), 3)
        )

    if not bindings:
        raise GltfAssetError("网格没有顶点")
    max_abs = max(abs(value) for position in positions_for_scale for value in position)
    scale = 1.0 if max_abs == 0.0 else float(target_peak) / max_abs
    scaled_vertices = tuple(
        AvatarMeshVertex(
            quantize_mesh_position(position, scale),
            indices,
            weights,
        )
        for position, indices, weights in bindings
    )
    inverse_bind_matrices = tuple(
        _quantize_inverse_bind_matrix(matrix, scale)
        for matrix in inverse_bind_source
    )
    return AvatarMeshAsset(
        name=name or (
            str(meshes[0].get("name") or asset_path.stem)
            if len(meshes) == 1
            else asset_path.stem
        ),
        source=source or str(asset_path),
        license_id=license_id,
        bone_names=bone_names,
        vertices=scaled_vertices,
        triangles=tuple(triangles),
        inverse_bind_matrices_q1_14=inverse_bind_matrices,
        triangle_colors_rgb=(tuple(triangle_colors) if has_explicit_material else ()),
        triangle_double_sided=tuple(triangle_double_sided),
    )


def _iter_mesh_primitives(
    meshes: Sequence[Mapping[str, Any]],
) -> Sequence[tuple[int, int, Any]]:
    return tuple(
        (mesh_index, primitive_index, primitive)
        for mesh_index, mesh in enumerate(meshes)
        for primitive_index, primitive in enumerate(mesh["primitives"])
    )


def _validate_mesh_nodes(document: Mapping[str, Any], mesh_count: int) -> None:
    """只接受可无损扁平化到同一绑定空间的 mesh 节点。"""

    nodes = document.get("nodes", ())
    if not isinstance(nodes, list):
        raise GltfAssetError("nodes 必须是数组")
    parents = _build_node_parent_map(nodes)
    references: dict[int, int] = {}
    for node_index, node in enumerate(nodes):
        if not isinstance(node, Mapping) or "mesh" not in node:
            continue
        mesh_index = node.get("mesh")
        if not isinstance(mesh_index, int) or not 0 <= mesh_index < mesh_count:
            raise GltfAssetError(f"node[{node_index}] mesh 索引无效")
        if mesh_index in references:
            raise GltfAssetError(
                f"mesh[{mesh_index}] 被多个 node 实例化；请在导出前应用实例"
            )
        references[mesh_index] = node_index
        skin_index = node.get("skin")
        if skin_index is not None and skin_index != 0:
            raise GltfAssetError(
                f"node[{node_index}] 使用 skin={skin_index}；当前资产必须统一为一个 skin"
            )
        current: int | None = node_index
        visited: set[int] = set()
        while current is not None:
            if current in visited:
                raise GltfAssetError("mesh 节点祖先层级包含循环")
            visited.add(current)
            current_node = nodes[current]
            if not isinstance(current_node, Mapping):
                raise GltfAssetError(f"node[{current}] 不是对象")
            if not _node_transform_is_identity(current_node):
                relation = "mesh " if current == node_index else "mesh 祖先"
                raise GltfAssetError(
                    f"node[{current}] 带有未应用的 {relation}变换；"
                    "请在导出前应用旋转、缩放和位移"
                )
            current = parents.get(current)


def _build_node_parent_map(nodes: Sequence[Any]) -> dict[int, int]:
    parents: dict[int, int] = {}
    for parent_index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise GltfAssetError(f"node[{parent_index}] 不是对象")
        children = node.get("children", ())
        if (
            not isinstance(children, Sequence)
            or isinstance(children, (str, bytes))
        ):
            raise GltfAssetError(f"node[{parent_index}].children 必须是数组")
        for child in children:
            if not isinstance(child, int) or not 0 <= child < len(nodes):
                raise GltfAssetError(f"node[{parent_index}] children 索引无效")
            if child in parents:
                raise GltfAssetError(f"node[{child}] 被多个父节点引用")
            parents[child] = parent_index
    return parents


def _node_transform_is_identity(node: Mapping[str, Any]) -> bool:
    if "matrix" in node:
        matrix = node["matrix"]
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        if not _numeric_sequence_close(matrix, identity):
            return False
    return (
        _numeric_sequence_close(node.get("translation", (0.0, 0.0, 0.0)), (0.0, 0.0, 0.0))
        and _numeric_sequence_close(node.get("rotation", (0.0, 0.0, 0.0, 1.0)), (0.0, 0.0, 0.0, 1.0))
        and _numeric_sequence_close(node.get("scale", (1.0, 1.0, 1.0)), (1.0, 1.0, 1.0))
    )


def _numeric_sequence_close(raw: Any, expected: tuple[float, ...]) -> bool:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or len(raw) != len(expected)
    ):
        return False
    try:
        values = tuple(float(value) for value in raw)
    except (TypeError, ValueError):
        return False
    return all(
        math.isfinite(value) and abs(value - reference) <= 1.0e-6
        for value, reference in zip(values, expected, strict=True)
    )


def _read_primitive_base_color(
    document: Mapping[str, Any],
    primitive: Mapping[str, Any],
    primitive_index: int,
) -> tuple[int, int, int] | None:
    """读取 glTF PBR 基础色；纹理仍由后续图形路径处理。"""

    material_index = primitive.get("material")
    if material_index is None:
        return None
    materials = document.get("materials")
    if (
        not isinstance(material_index, int)
        or not isinstance(materials, list)
        or material_index < 0
        or material_index >= len(materials)
        or not isinstance(materials[material_index], Mapping)
    ):
        raise GltfAssetError(f"primitive[{primitive_index}] material 索引无效")
    material = materials[material_index]
    pbr = material.get("pbrMetallicRoughness", {})
    if not isinstance(pbr, Mapping):
        raise GltfAssetError(f"primitive[{primitive_index}] PBR 材质无效")
    factor = pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))
    if (
        not isinstance(factor, Sequence)
        or isinstance(factor, (str, bytes))
        or len(factor) != 4
    ):
        raise GltfAssetError(f"primitive[{primitive_index}] baseColorFactor 必须为 RGBA")
    values = tuple(float(value) for value in factor)
    if any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in values):
        raise GltfAssetError(f"primitive[{primitive_index}] baseColorFactor 必须位于 [0, 1]")
    return tuple(round(value * 255.0) for value in values[:3])  # type: ignore[return-value]


def _read_primitive_double_sided(
    document: Mapping[str, Any],
    primitive: Mapping[str, Any],
    primitive_index: int,
) -> bool:
    material_index = primitive.get("material")
    if material_index is None:
        return False
    materials = document.get("materials")
    if (
        not isinstance(material_index, int)
        or not isinstance(materials, list)
        or material_index < 0
        or material_index >= len(materials)
        or not isinstance(materials[material_index], Mapping)
    ):
        raise GltfAssetError(f"primitive[{primitive_index}] material 索引无效")
    value = materials[material_index].get("doubleSided", False)
    if not isinstance(value, bool):
        raise GltfAssetError(f"primitive[{primitive_index}] doubleSided 必须为布尔值")
    return value


def _build_bone_palette(
    bone_names: tuple[str, ...],
    inverse_bind_matrices: tuple[tuple[float, ...], ...],
    bone_name_map: Mapping[str, str] | None,
    bone_representatives: Mapping[str, str] | None,
) -> tuple[tuple[str, ...], tuple[int, ...], tuple[tuple[float, ...], ...]]:
    """构建不超过 16 槽的目标骨架，并允许显式多对一简化。"""

    if len(inverse_bind_matrices) != len(bone_names):
        raise GltfAssetError("inverse-bind 数量必须与源骨骼一致")
    bone_name_map = bone_name_map or {}
    source_names = set(bone_names)
    unknown_sources = tuple(sorted(set(bone_name_map) - source_names))
    if unknown_sources:
        raise GltfAssetError(
            "骨骼名称映射引用了资产中不存在的名称：" + ", ".join(unknown_sources)
        )
    mapped = tuple(str(bone_name_map.get(name, name)).strip() for name in bone_names)
    if any(not name for name in mapped):
        raise GltfAssetError("骨骼名称映射的目标名称不能为空")
    palette: list[str] = []
    source_to_palette: list[int] = []
    for source_index, target_name in enumerate(mapped):
        if target_name not in palette:
            palette.append(target_name)
        source_to_palette.append(palette.index(target_name))
    if len(palette) > MATRIX_COUNT:
        raise GltfAssetError(
            f"映射后仍有 {len(palette)} 根骨骼，当前 PL 矩阵表最多 {MATRIX_COUNT} 根；"
            "请用 --bone-map 把手指、脚趾等附属骨骼合并到对应肢段"
        )
    representatives = bone_representatives or {}
    unknown_targets = sorted(set(representatives) - set(palette))
    unknown_representatives = sorted(set(representatives.values()) - source_names)
    if unknown_targets:
        raise GltfAssetError("代表骨骼引用了不存在的目标：" + ", ".join(unknown_targets))
    if unknown_representatives:
        raise GltfAssetError(
            "代表骨骼引用了不存在的源名称：" + ", ".join(unknown_representatives)
        )
    palette_inverse_bind: list[tuple[float, ...]] = []
    for target_name in palette:
        source_name = representatives.get(target_name)
        if source_name is None:
            source_index = mapped.index(target_name)
        else:
            source_index = bone_names.index(source_name)
            if mapped[source_index] != target_name:
                raise GltfAssetError(
                    f"源骨骼 {source_name} 没有映射到目标 {target_name}，不能作为其代表"
                )
        palette_inverse_bind.append(inverse_bind_matrices[source_index])
    return tuple(palette), tuple(source_to_palette), tuple(palette_inverse_bind)


def _merge_vertex_influences(
    source_indices: tuple[int, ...],
    source_weights: tuple[float, ...],
    source_to_palette: tuple[int, ...],
) -> tuple[tuple[int, int, int, int], tuple[float, float, float, float]]:
    """合并映射到同一目标骨骼的权重，并保留最强四项影响。"""

    merged: dict[int, float] = {}
    for source_index, weight in zip(source_indices, source_weights, strict=True):
        if not math.isfinite(weight) or weight < 0.0:
            raise GltfAssetError("WEIGHTS_0 必须包含有限非负数")
        target_index = source_to_palette[source_index]
        merged[target_index] = merged.get(target_index, 0.0) + weight
    strongest = sorted(merged.items(), key=lambda item: (-item[1], item[0]))[:4]
    if not strongest or sum(weight for _, weight in strongest) <= 0.0:
        raise GltfAssetError("顶点蒙皮权重之和必须大于零")
    padded = strongest + [(0, 0.0)] * (4 - len(strongest))
    return (
        tuple(index for index, _ in padded),  # type: ignore[return-value]
        tuple(weight for _, weight in padded),  # type: ignore[return-value]
    )


def _read_document(path: Path) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    if path.suffix.lower() == ".glb":
        raw = path.read_bytes()
        if len(raw) < 12 or raw[:4] != b"glTF":
            raise GltfAssetError("GLB 文件头无效")
        version, total_length = struct.unpack_from("<II", raw, 4)
        if version != 2 or total_length > len(raw):
            raise GltfAssetError("只支持完整的 glTF 2.0 GLB")
        json_chunk: bytes | None = None
        binary_chunks: list[bytes] = []
        offset = 12
        while offset + 8 <= total_length:
            chunk_length, chunk_type = struct.unpack_from("<II", raw, offset)
            offset += 8
            chunk = raw[offset : offset + chunk_length]
            offset += chunk_length
            if len(chunk) != chunk_length:
                raise GltfAssetError("GLB chunk 长度越界")
            if chunk_type == 0x4E4F534A:
                json_chunk = chunk.rstrip(b" \t\r\n\x00")
            elif chunk_type == 0x004E4942:
                binary_chunks.append(chunk)
        if json_chunk is None:
            raise GltfAssetError("GLB 缺少 JSON chunk")
        try:
            document = json.loads(json_chunk.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GltfAssetError("GLB JSON chunk 无效") from exc
        if not isinstance(document, dict):
            raise GltfAssetError("glTF JSON 根节点必须是对象")
        return document, tuple(binary_chunks)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GltfAssetError(f"无法读取 glTF JSON：{path}") from exc
    if not isinstance(document, dict):
        raise GltfAssetError("glTF JSON 根节点必须是对象")
    buffers = tuple(_read_buffer_uri(path.parent, item) for item in document.get("buffers", ()))
    return document, buffers


def _read_buffer_uri(base: Path, raw: Any) -> bytes:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("uri"), str):
        raise GltfAssetError(".gltf 外部 buffer 必须包含 uri")
    uri = raw["uri"]
    parsed = urlparse(uri)
    if parsed.scheme == "data":
        header, separator, payload = uri.partition(",")
        if not separator or ";base64" not in header.lower():
            raise GltfAssetError("只支持 base64 data URI buffer")
        try:
            return base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise GltfAssetError("data URI buffer 的 base64 无效") from exc
    if parsed.scheme or parsed.netloc:
        raise GltfAssetError("不允许从网络加载 glTF buffer")
    path = (base / unquote(uri)).resolve()
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GltfAssetError(f"无法读取 glTF buffer：{path}") from exc


def _read_skin(
    document: Mapping[str, Any],
    buffers: Sequence[bytes],
) -> tuple[tuple[str, ...], tuple[tuple[float, ...], ...]]:
    skins = document.get("skins")
    if not isinstance(skins, list) or not skins:
        identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        return ("root",), (identity,)
    if len(skins) != 1:
        raise GltfAssetError(
            f"资产包含 {len(skins)} 个 skin；请在导出前统一为一个人体骨架"
        )
    skin = skins[0]
    if not isinstance(skin, Mapping) or not isinstance(skin.get("joints"), list):
        raise GltfAssetError("第一个 skin 缺少 joints")
    nodes = document.get("nodes", [])
    names: list[str] = []
    for index in skin["joints"]:
        if not isinstance(index, int) or index < 0 or index >= len(nodes):
            raise GltfAssetError("skin joints 引用了不存在的 node")
        node = nodes[index]
        node_name = node.get("name") if isinstance(node, Mapping) else None
        names.append(str(node_name) if node_name else f"bone_{len(names)}")
    if not names:
        raise GltfAssetError("skin 至少需要一根关节")
    inverse_accessor = skin.get("inverseBindMatrices")
    if inverse_accessor is None:
        identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        return tuple(names), tuple(identity for _ in names)
    matrices = _read_accessor(
        document,
        buffers,
        inverse_accessor,
        "inverseBindMatrices",
    )
    if len(matrices) != len(names) or any(len(matrix) != 16 for matrix in matrices):
        raise GltfAssetError("inverseBindMatrices 必须为每根 skin 关节提供一个 MAT4")
    result: list[tuple[float, ...]] = []
    for matrix in matrices:
        values = tuple(float(value) for value in matrix)
        if not all(math.isfinite(value) for value in values):
            raise GltfAssetError("inverseBindMatrices 必须包含有限数值")
        # glTF stores matrices column-major. Runtime matrices are row-major 3x4.
        if any(abs(values[index]) > 1.0e-6 for index in (3, 7, 11)) or abs(values[15] - 1.0) > 1.0e-6:
            raise GltfAssetError("inverseBindMatrices 必须是仿射 MAT4")
        result.append(
            (
                values[0], values[4], values[8], values[12],
                values[1], values[5], values[9], values[13],
                values[2], values[6], values[10], values[14],
            )
        )
    return tuple(names), tuple(result)


def _quantize_inverse_bind_matrix(
    matrix: tuple[float, ...],
    vertex_scale: float,
) -> AffineMatrixQ1_14:
    values = tuple(
        _quantize_matrix_value(
            value * vertex_scale if index in (3, 7, 11) else value
        )
        for index, value in enumerate(matrix)
    )
    return AffineMatrixQ1_14(values)  # type: ignore[arg-type]


def _quantize_matrix_value(value: float) -> int:
    scaled = value * (1 << 14)
    rounded = math.floor(scaled + 0.5) if scaled >= 0.0 else math.ceil(scaled - 0.5)
    if rounded < -(1 << 15) or rounded > (1 << 15) - 1:
        raise GltfAssetError("inverse-bind 系数超出有符号 Q1.14 范围")
    return rounded


def _read_optional_accessor(
    document: Mapping[str, Any], buffers: Sequence[bytes], index: Any, name: str
) -> list[tuple[Any, ...]] | None:
    return None if index is None else _read_accessor(document, buffers, index, name)


def _read_accessor(
    document: Mapping[str, Any], buffers: Sequence[bytes], index: Any, name: str
) -> list[tuple[Any, ...]]:
    accessors = document.get("accessors")
    views = document.get("bufferViews")
    if not isinstance(index, int) or not isinstance(accessors, list) or not isinstance(views, list):
        raise GltfAssetError(f"{name} accessor 索引无效")
    if index < 0 or index >= len(accessors):
        raise GltfAssetError(f"{name} accessor 越界")
    accessor = accessors[index]
    if not isinstance(accessor, Mapping) or "bufferView" not in accessor:
        raise GltfAssetError(f"{name} 不支持 sparse 或缺少 bufferView")
    view_index = accessor["bufferView"]
    if not isinstance(view_index, int) or view_index < 0 or view_index >= len(views):
        raise GltfAssetError(f"{name} bufferView 越界")
    view = views[view_index]
    if not isinstance(view, Mapping):
        raise GltfAssetError(f"{name} bufferView 无效")
    buffer_index = view.get("buffer")
    if not isinstance(buffer_index, int) or buffer_index < 0 or buffer_index >= len(buffers):
        raise GltfAssetError(f"{name} buffer 索引无效")
    component_type = accessor.get("componentType")
    info = _COMPONENTS.get(component_type)
    shape = accessor.get("type")
    count = accessor.get("count")
    if info is None or shape not in _TYPE_COUNTS or not isinstance(count, int) or count < 1:
        raise GltfAssetError(f"{name} accessor 类型不受支持")
    if accessor.get("normalized") and component_type == 5126:
        raise GltfAssetError(f"{name} 浮点 accessor 不能标记 normalized")
    fmt, component_size, is_float = info
    component_count = _TYPE_COUNTS[shape]
    element_size = component_size * component_count
    stride = int(view.get("byteStride", element_size))
    if stride < element_size:
        raise GltfAssetError(f"{name} byteStride 小于元素长度")
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    blob = buffers[buffer_index]
    if start < 0 or start + (count - 1) * stride + element_size > len(blob):
        raise GltfAssetError(f"{name} accessor 超出 buffer 长度")
    normalized = bool(accessor.get("normalized", False))
    result: list[tuple[Any, ...]] = []
    for item_index in range(count):
        item_start = start + item_index * stride
        values = struct.unpack_from("<" + fmt * component_count, blob, item_start)
        if normalized and not is_float:
            maximum = (1 << (component_size * 8)) - 1
            values = tuple(value / maximum for value in values)
        result.append(tuple(values))
    return result


def _read_indices(
    document: Mapping[str, Any], buffers: Sequence[bytes], index: Any, vertex_count: int
) -> list[int]:
    if index is None:
        return list(range(vertex_count))
    values = _read_accessor(document, buffers, index, "indices")
    if any(len(value) != 1 for value in values):
        raise GltfAssetError("indices 必须是 SCALAR")
    result = [int(value[0]) for value in values]
    if any(value < 0 or value >= vertex_count for value in result):
        raise GltfAssetError("indices 超出 primitive 顶点范围")
    return result


__all__ = ["GltfAssetError", "load_gltf_mesh_asset"]
