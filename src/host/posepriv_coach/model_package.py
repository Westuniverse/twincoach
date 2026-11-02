"""Static contract checks for the checked-in B3136 MoveNet model package.

The board still needs VART/XIR to inspect tensor shapes at runtime.  This module
checks the files that are available before booting Linux: ``.xmodel`` presence,
the adjacent ``meta.json`` and the NTD ``.prototxt`` post-processing contract.
It deliberately does not hash or copy the model and does not replace the
runtime VART inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping


class B3136ModelPackageError(ValueError):
    """Raised when a MoveNet B3136 package is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class B3136ModelPackageContract:
    """The model-package fields required by the unified DPU deployment."""

    model_path: Path
    meta_path: Path
    prototxt_path: Path
    target: str
    filename: str
    kernel: tuple[str, ...]
    model_type: str
    layer_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    confidence_threshold: float
    center_weight_count: int
    model_size_bytes: int
    dpu_arch: int

    def to_record(self) -> dict[str, Any]:
        """Return JSON-safe evidence for a preflight report."""

        return {
            "model_path": str(self.model_path),
            "meta_path": str(self.meta_path),
            "prototxt_path": str(self.prototxt_path),
            "target": self.target,
            "filename": self.filename,
            "kernel": list(self.kernel),
            "model_type": self.model_type,
            "layer_names": list(self.layer_names),
            "means": list(self.means),
            "scales": list(self.scales),
            "confidence_threshold": self.confidence_threshold,
            "center_weight_count": self.center_weight_count,
            "model_size_bytes": self.model_size_bytes,
            "dpu_arch": self.dpu_arch,
        }


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_MEAN_RE = re.compile(rf"^\s*mean\s*:\s*({_NUMBER})\s*$", re.MULTILINE)
_SCALE_RE = re.compile(rf"^\s*scale\s*:\s*({_NUMBER})\s*$", re.MULTILINE)
_MODEL_TYPE_RE = re.compile(r"^\s*model_type\s*:\s*([A-Za-z0-9_]+)\s*$", re.MULTILINE)
_THRESHOLD_RE = re.compile(rf"^\s*conf_threshold\s*:\s*({_NUMBER})\s*$", re.MULTILINE)
_LAYER_RE = re.compile(r'^\s*layer_name\s*:\s*"([^"]+)"\s*$', re.MULTILINE)
_CENTER_WEIGHT_RE = re.compile(r"^\s*center_weight\s*:\s*", re.MULTILINE)
_TARGET_ARCH_RE = re.compile(r"(?:^|_)B(\d+)$")

EXPECTED_NTD_LAYERS = ("heatmap", "center", "reg", "offset")
EXPECTED_MODEL_TYPE = "MOVENET"
EXPECTED_MEANS = (127.5, 127.5, 127.5)
EXPECTED_SCALE = 0.00784314
EXPECTED_CENTER_WEIGHT_COUNT = 2304


def _single_match(pattern: re.Pattern[str], text: str, field: str) -> str:
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise B3136ModelPackageError(
            f"prototxt 字段 {field} 应出现 1 次，实际为 {len(matches)} 次"
        )
    return matches[0]


def _parse_target_arch(target: str) -> int:
    match = _TARGET_ARCH_RE.search(target)
    if match is None:
        raise B3136ModelPackageError(f"无法从 DPU target 解析架构编号：{target!r}")
    return int(match.group(1))


def parse_b3136_model_package(
    model_path: Path,
    meta_text: str,
    prototxt_text: str,
    *,
    model_size_bytes: int = 1,
    expected_target: str = "DPUCZDX8G_ISA1_B3136",
) -> B3136ModelPackageContract:
    """Parse and validate package text without requiring XIR.

    ``model_size_bytes`` is injectable so unit tests can validate the contract
    without checking in a binary model.  The normal file-based entry point
    obtains it from the actual xmodel file.
    """

    if model_size_bytes <= 0:
        raise B3136ModelPackageError("xmodel 文件为空")
    try:
        metadata: Mapping[str, Any] = json.loads(meta_text)
    except json.JSONDecodeError as exc:
        raise B3136ModelPackageError(f"meta.json 不是有效 JSON：{exc}") from exc
    if not isinstance(metadata, Mapping):
        raise B3136ModelPackageError("meta.json 顶层必须是对象")

    target = metadata.get("target")
    filename = metadata.get("filename")
    kernel_value = metadata.get("kernel")
    errors: list[str] = []
    if target != expected_target:
        errors.append(f"target={target!r}，期望 {expected_target!r}")
    if filename != model_path.name:
        errors.append(f"filename={filename!r}，期望 {model_path.name!r}")
    if not isinstance(kernel_value, list) or not all(
        isinstance(item, str) and item for item in kernel_value
    ):
        errors.append("kernel 必须是非空字符串数组")
        kernel: tuple[str, ...] = ()
    else:
        kernel = tuple(kernel_value)
    if errors:
        raise B3136ModelPackageError("meta.json 契约不匹配：" + "；".join(errors))

    model_type = _single_match(_MODEL_TYPE_RE, prototxt_text, "model_type")
    means = tuple(float(value) for value in _MEAN_RE.findall(prototxt_text))
    scales = tuple(float(value) for value in _SCALE_RE.findall(prototxt_text))
    layer_names = tuple(_LAYER_RE.findall(prototxt_text))
    threshold = float(_single_match(_THRESHOLD_RE, prototxt_text, "conf_threshold"))
    center_weight_count = len(_CENTER_WEIGHT_RE.findall(prototxt_text))

    field_errors: list[str] = []
    if model_type != EXPECTED_MODEL_TYPE:
        field_errors.append(f"model_type={model_type!r}，期望 {EXPECTED_MODEL_TYPE!r}")
    if len(means) != 3 or any(
        not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-6)
        for value, expected in zip(means, EXPECTED_MEANS)
    ):
        field_errors.append(f"mean={means!r}，期望三通道 {EXPECTED_MEANS!r}")
    if len(scales) != 3 or any(
        not math.isclose(value, EXPECTED_SCALE, rel_tol=0.0, abs_tol=1e-7)
        for value in scales
    ):
        field_errors.append(f"scale={scales!r}，期望三通道约 {EXPECTED_SCALE}")
    if layer_names != EXPECTED_NTD_LAYERS:
        field_errors.append(f"layer_name={layer_names!r}，期望 {EXPECTED_NTD_LAYERS!r}")
    if not 0.0 <= threshold <= 1.0:
        field_errors.append(f"conf_threshold={threshold} 不在 [0, 1]")
    if center_weight_count != EXPECTED_CENTER_WEIGHT_COUNT:
        field_errors.append(
            f"center_weight 数量={center_weight_count}，期望 {EXPECTED_CENTER_WEIGHT_COUNT}"
        )
    if field_errors:
        raise B3136ModelPackageError("prototxt 契约不匹配：" + "；".join(field_errors))

    dpu_arch = _parse_target_arch(str(target))
    return B3136ModelPackageContract(
        model_path=model_path,
        meta_path=model_path.with_name("meta.json"),
        prototxt_path=model_path.with_suffix(".prototxt"),
        target=str(target),
        filename=str(filename),
        kernel=kernel,
        model_type=model_type,
        layer_names=layer_names,
        means=means,
        scales=scales,
        confidence_threshold=threshold,
        center_weight_count=center_weight_count,
        model_size_bytes=model_size_bytes,
        dpu_arch=dpu_arch,
    )


def inspect_b3136_model_package(
    model_path: Path,
    *,
    expected_target: str = "DPUCZDX8G_ISA1_B3136",
) -> B3136ModelPackageContract:
    """Validate an on-disk xmodel plus its adjacent metadata files."""

    model_path = Path(model_path)
    if not model_path.is_file():
        raise B3136ModelPackageError(f"找不到 xmodel：{model_path}")
    meta_path = model_path.with_name("meta.json")
    prototxt_path = model_path.with_suffix(".prototxt")
    missing = [str(path) for path in (meta_path, prototxt_path) if not path.is_file()]
    if missing:
        raise B3136ModelPackageError("模型包缺少文件：" + ", ".join(missing))
    try:
        meta_text = meta_path.read_text(encoding="utf-8")
        prototxt_text = prototxt_path.read_text(encoding="utf-8")
        model_size = model_path.stat().st_size
    except OSError as exc:
        raise B3136ModelPackageError(f"读取模型包失败：{exc}") from exc
    return parse_b3136_model_package(
        model_path,
        meta_text,
        prototxt_text,
        model_size_bytes=model_size,
        expected_target=expected_target,
    )

