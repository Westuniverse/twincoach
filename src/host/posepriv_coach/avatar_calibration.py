"""双摄三维姿态标定参数的版本化 JSON 契约。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .avatar_pipeline import AvatarPipelineConfig
from .calibration_coverage import CalibrationViewCoverage
from .triangulation import CameraExtrinsics, CameraIntrinsics
from .rectified_stereo import RectificationCalibration


@dataclass(frozen=True, slots=True)
class CalibrationCaptureGeometry:
    """标定图像与实际采集图像之间的等比例缩放关系。"""

    front_scale: float
    side_scale: float
    aspect_ratio_relative_error: float

    def as_report_mapping(self) -> dict[str, float | bool]:
        return {
            "uniform_scaling_compatible": True,
            "front_scale": self.front_scale,
            "side_scale": self.side_scale,
            "aspect_ratio_relative_error": self.aspect_ratio_relative_error,
        }


@dataclass(frozen=True, slots=True)
class StereoCalibrationQuality:
    """可写入匿名会话报告的双目几何质量摘要。"""

    baseline_m: float
    epipolar_mean_px: float
    epipolar_p95_px: float
    epipolar_max_px: float
    sample_count: int
    front_view_coverage: CalibrationViewCoverage | None = None
    side_view_coverage: CalibrationViewCoverage | None = None

    def __post_init__(self) -> None:
        values = (
            self.baseline_m,
            self.epipolar_mean_px,
            self.epipolar_p95_px,
            self.epipolar_max_px,
        )
        if any(value < 0.0 for value in values):
            raise ValueError("stereo calibration quality values must be non-negative")
        if self.sample_count < 1:
            raise ValueError("stereo calibration quality sample_count must be positive")

    def as_report_mapping(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "baseline_m": self.baseline_m,
            "epipolar_mean_px": self.epipolar_mean_px,
            "epipolar_p95_px": self.epipolar_p95_px,
            "epipolar_max_px": self.epipolar_max_px,
            "sample_count": self.sample_count,
        }
        if self.front_view_coverage is not None and self.side_view_coverage is not None:
            report["chessboard_view_coverage"] = {
                "front": self.front_view_coverage.to_record(),
                "side": self.side_view_coverage.to_record(),
            }
        return report


@dataclass(frozen=True, slots=True)
class AvatarCalibration:
    """正面/侧面相机内外参与世界坐标单位。"""

    schema_version: int
    coordinate_unit: str
    front_intrinsics: CameraIntrinsics
    side_intrinsics: CameraIntrinsics
    front_extrinsics: CameraExtrinsics
    side_extrinsics: CameraExtrinsics
    quality: StereoCalibrationQuality | None = None
    rectification: RectificationCalibration | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported avatar calibration schema")
        if not self.coordinate_unit:
            raise ValueError("coordinate_unit must not be empty")

    def to_pipeline_config(self, **options: object) -> AvatarPipelineConfig:
        """将标定数据接到现有三角测量/蒙皮计划。"""

        options.setdefault("rectification", self.rectification)
        return AvatarPipelineConfig(
            front_intrinsics=self.front_intrinsics,
            side_intrinsics=self.side_intrinsics,
            front_extrinsics=self.front_extrinsics,
            side_extrinsics=self.side_extrinsics,
            **options,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AvatarCalibration":
        if not isinstance(raw, Mapping):
            raise ValueError("avatar calibration must be a mapping")
        version = raw.get("schema_version", 1)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("schema_version must be an integer")
        if version != 1:
            raise ValueError("unsupported avatar calibration schema")
        coordinate_unit = raw.get("coordinate_unit", "meter")
        if not isinstance(coordinate_unit, str):
            raise ValueError("coordinate_unit must be text")
        return cls(
            schema_version=version,
            coordinate_unit=coordinate_unit,
            front_intrinsics=_parse_intrinsics(raw, "front"),
            side_intrinsics=_parse_intrinsics(raw, "side"),
            front_extrinsics=_parse_extrinsics(raw, "front"),
            side_extrinsics=_parse_extrinsics(raw, "side"),
            quality=_parse_stereo_quality(raw),
            rectification=_parse_rectification(raw),
        )


def load_avatar_calibration(path: str | Path) -> AvatarCalibration:
    """从 UTF-8 JSON 文件读取一次已经完成的双摄标定结果。"""

    calibration_path = Path(path)
    try:
        raw = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read avatar calibration: {calibration_path}") from exc
    return AvatarCalibration.from_mapping(raw)


def validate_calibration_capture_geometry(
    calibration: AvatarCalibration,
    *,
    front_width: int,
    front_height: int,
    side_width: int,
    side_height: int,
    aspect_ratio_tolerance: float = 0.005,
) -> CalibrationCaptureGeometry:
    """确认实际双摄模式可由标定内参做等比例缩放得到。

    归一化关键点允许分辨率整体变化，例如 1920×1080 标定用于 1280×720；
    但不允许宽高比改变，因为这通常意味着裁剪或另一套视场。
    """

    dimensions = (front_width, front_height, side_width, side_height)
    if any(value <= 0 for value in dimensions):
        raise ValueError("实际双摄宽高必须为正数")
    if not 0.0 <= aspect_ratio_tolerance <= 0.1:
        raise ValueError("宽高比容差必须在 [0, 0.1] 范围内")

    front_scale = _uniform_image_scale(
        calibration.front_intrinsics,
        front_width,
        front_height,
        "正面",
        aspect_ratio_tolerance,
    )
    side_scale = _uniform_image_scale(
        calibration.side_intrinsics,
        side_width,
        side_height,
        "侧面",
        aspect_ratio_tolerance,
    )
    front_aspect = front_width / front_height
    side_aspect = side_width / side_height
    pair_error = abs(front_aspect - side_aspect) / max(front_aspect, side_aspect)
    if pair_error > aspect_ratio_tolerance:
        raise ValueError(
            "正面与侧面实际采集宽高比不一致，不能用于同一双目标定："
            f"{front_width}x{front_height} 与 {side_width}x{side_height}"
        )
    return CalibrationCaptureGeometry(front_scale, side_scale, pair_error)


def _uniform_image_scale(
    intrinsics: CameraIntrinsics,
    width: int,
    height: int,
    label: str,
    tolerance: float,
) -> float:
    scale_x = width / intrinsics.width
    scale_y = height / intrinsics.height
    relative_error = abs(scale_x - scale_y) / max(scale_x, scale_y)
    if relative_error > tolerance:
        raise ValueError(
            f"{label}摄像头实际模式 {width}x{height} 与标定模式 "
            f"{intrinsics.width}x{intrinsics.height} 宽高比不一致；"
            "请使用标定时的视场模式或重新标定"
        )
    return (scale_x + scale_y) * 0.5


def _parse_intrinsics(raw: Mapping[str, Any], view: str) -> CameraIntrinsics:
    camera = _camera_mapping(raw, view)
    intrinsic = camera.get("intrinsics")
    if not isinstance(intrinsic, Mapping):
        raise ValueError(f"{view}.intrinsics must be a mapping")
    try:
        return CameraIntrinsics(
            width=_required_int(intrinsic, "width"),
            height=_required_int(intrinsic, "height"),
            fx=_required_float(intrinsic, "fx"),
            fy=_required_float(intrinsic, "fy"),
            cx=_required_float(intrinsic, "cx"),
            cy=_required_float(intrinsic, "cy"),
            distortion=_optional_number_sequence(
                intrinsic.get("distortion"), 5, "distortion"
            ),
        )
    except ValueError as exc:
        raise ValueError(f"{view}.intrinsics: {exc}") from exc


def _parse_extrinsics(raw: Mapping[str, Any], view: str) -> CameraExtrinsics:
    camera = _camera_mapping(raw, view)
    extrinsic = camera.get("extrinsics")
    if not isinstance(extrinsic, Mapping):
        raise ValueError(f"{view}.extrinsics must be a mapping")
    try:
        rotation = _number_sequence(
            extrinsic.get("rotation_camera_to_world"), 9, "rotation"
        )
        center = _number_sequence(extrinsic.get("center_world"), 3, "center_world")
        return CameraExtrinsics(rotation_camera_to_world=rotation, center_world=center)
    except ValueError as exc:
        raise ValueError(f"{view}.extrinsics: {exc}") from exc


def _parse_stereo_quality(
    raw: Mapping[str, Any],
) -> StereoCalibrationQuality | None:
    quality = raw.get("stereo_quality")
    if quality is None:
        return None
    if not isinstance(quality, Mapping):
        raise ValueError("stereo_quality must be a mapping")
    epipolar = quality.get("symmetric_epipolar_error_px")
    if not isinstance(epipolar, Mapping):
        raise ValueError("stereo_quality symmetric epipolar error must be a mapping")
    front_coverage, side_coverage = _parse_view_coverage_pair(quality)
    return StereoCalibrationQuality(
        baseline_m=_required_float(quality, "baseline_m"),
        epipolar_mean_px=_required_float(epipolar, "mean"),
        epipolar_p95_px=_required_float(epipolar, "p95"),
        epipolar_max_px=_required_float(epipolar, "max"),
        sample_count=_required_int(epipolar, "sample_count"),
        front_view_coverage=front_coverage,
        side_view_coverage=side_coverage,
    )


def _parse_rectification(raw: Mapping[str, Any]) -> RectificationCalibration | None:
    value = raw.get("rectification")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("rectification must be a mapping")
    version = value.get("schema_version", 1)
    if version != 1:
        raise ValueError("unsupported rectification schema")
    try:
        return RectificationCalibration(
            front_rotation_camera_to_rectified=_number_sequence(
                value.get("front_rotation_camera_to_rectified"),
                9,
                "front_rotation_camera_to_rectified",
            ),
            side_rotation_camera_to_rectified=_number_sequence(
                value.get("side_rotation_camera_to_rectified"),
                9,
                "side_rotation_camera_to_rectified",
            ),
            baseline=_required_float(value, "baseline_m"),
        )
    except ValueError as exc:
        raise ValueError(f"rectification: {exc}") from exc


def _parse_view_coverage_pair(
    quality: Mapping[str, Any],
) -> tuple[CalibrationViewCoverage | None, CalibrationViewCoverage | None]:
    coverage = quality.get("chessboard_view_coverage")
    if coverage is None:
        return None, None
    if not isinstance(coverage, Mapping):
        raise ValueError("stereo_quality chessboard_view_coverage must be a mapping")
    return (
        _parse_view_coverage(coverage, "front"),
        _parse_view_coverage(coverage, "side"),
    )


def _parse_view_coverage(
    coverage: Mapping[str, Any], view: str
) -> CalibrationViewCoverage:
    raw = coverage.get(view)
    if not isinstance(raw, Mapping):
        raise ValueError(f"chessboard_view_coverage requires a {view} mapping")
    try:
        return CalibrationViewCoverage(
            sample_count=_required_int(raw, "sample_count"),
            centroid_x_min=_required_float(raw, "centroid_x_min"),
            centroid_x_max=_required_float(raw, "centroid_x_max"),
            centroid_y_min=_required_float(raw, "centroid_y_min"),
            centroid_y_max=_required_float(raw, "centroid_y_max"),
            board_area_fraction_min=_required_float(raw, "board_area_fraction_min"),
            board_area_fraction_max=_required_float(raw, "board_area_fraction_max"),
        )
    except ValueError as exc:
        raise ValueError(f"chessboard_view_coverage.{view}: {exc}") from exc


def _camera_mapping(raw: Mapping[str, Any], view: str) -> Mapping[str, Any]:
    camera = raw.get(view)
    if not isinstance(camera, Mapping):
        raise ValueError(f"calibration requires a {view} camera mapping")
    return camera


def _required_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _required_float(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _number_sequence(value: Any, length: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} numbers")
    if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in value):
        raise ValueError(f"{name} must contain exactly {length} numbers")
    return tuple(float(item) for item in value)


def _optional_number_sequence(
    value: Any, length: int, name: str
) -> tuple[float, ...]:
    if value is None:
        return ()
    return _number_sequence(value, length, name)


__all__ = [
    "AvatarCalibration",
    "CalibrationCaptureGeometry",
    "StereoCalibrationQuality",
    "load_avatar_calibration",
    "validate_calibration_capture_geometry",
]
