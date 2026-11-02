"""双目标定棋盘格在画面中的覆盖摘要。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class CalibrationViewCoverage:
    sample_count: int
    centroid_x_min: float
    centroid_x_max: float
    centroid_y_min: float
    centroid_y_max: float
    board_area_fraction_min: float
    board_area_fraction_max: float

    def __post_init__(self) -> None:
        if self.sample_count < 1:
            raise ValueError("calibration coverage sample_count must be positive")
        values = (
            self.centroid_x_min,
            self.centroid_x_max,
            self.centroid_y_min,
            self.centroid_y_max,
            self.board_area_fraction_min,
            self.board_area_fraction_max,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("calibration coverage values must be finite")
        if not all(0.0 <= value <= 1.0 for value in values):
            raise ValueError("calibration coverage values must be within [0, 1]")
        if self.centroid_x_min > self.centroid_x_max:
            raise ValueError("calibration coverage x range is reversed")
        if self.centroid_y_min > self.centroid_y_max:
            raise ValueError("calibration coverage y range is reversed")
        if self.board_area_fraction_min > self.board_area_fraction_max:
            raise ValueError("calibration coverage area range is reversed")

    @property
    def centroid_x_span(self) -> float:
        return self.centroid_x_max - self.centroid_x_min

    @property
    def centroid_y_span(self) -> float:
        return self.centroid_y_max - self.centroid_y_min

    def to_record(self) -> dict[str, float | int]:
        record = asdict(self)
        record["centroid_x_span"] = self.centroid_x_span
        record["centroid_y_span"] = self.centroid_y_span
        return record


def summarize_calibration_view_coverage(
    samples: Iterable[Sequence[tuple[float, float]]],
    *,
    image_width: int,
    image_height: int,
) -> CalibrationViewCoverage:
    """汇总棋盘中心移动与包围盒面积，不把经验范围变成通过门禁。"""

    if image_width <= 0 or image_height <= 0:
        raise ValueError("标定图像宽高必须为正数")
    centroids: list[tuple[float, float]] = []
    areas: list[float] = []
    for sample in samples:
        if len(sample) < 4:
            raise ValueError("每个棋盘覆盖样本至少需要四个角点")
        points = tuple((float(x), float(y)) for x, y in sample)
        if not all(math.isfinite(value) for point in points for value in point):
            raise ValueError("棋盘角点必须为有限数值")
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
        centroids.append(
            (
                sum(xs) / len(xs) / image_width,
                sum(ys) / len(ys) / image_height,
            )
        )
        areas.append(
            (max(xs) - min(xs))
            * (max(ys) - min(ys))
            / (image_width * image_height)
        )
    if not centroids:
        raise ValueError("至少需要一个棋盘覆盖样本")
    return CalibrationViewCoverage(
        sample_count=len(centroids),
        centroid_x_min=min(value[0] for value in centroids),
        centroid_x_max=max(value[0] for value in centroids),
        centroid_y_min=min(value[1] for value in centroids),
        centroid_y_max=max(value[1] for value in centroids),
        board_area_fraction_min=min(areas),
        board_area_fraction_max=max(areas),
    )


__all__ = ["CalibrationViewCoverage", "summarize_calibration_view_coverage"]
