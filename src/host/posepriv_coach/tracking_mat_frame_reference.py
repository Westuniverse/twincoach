# SPDX-License-Identifier: MIT
"""Independent row-alias address golden for the private full Mat frame writer."""
from dataclasses import dataclass

from .tracking_pyramid_reference import HALO, reflect101, tracking_pyramid_layout


@dataclass(frozen=True)
class MatRowDestination:
    level: int
    source_y: int
    destination_y: int
    gray_address: int
    gradient_address: int
    gray_bytes: int
    gradient_bytes: int
    zero_gradient: bool


@dataclass(frozen=True)
class MatFrameWritePlan:
    rows: tuple[MatRowDestination, ...]
    padding: tuple[tuple[int, int], ...]
    frame_bytes: int


def mat_frame_write_plan(width: int, height: int, base: int) -> MatFrameWritePlan:
    """Enumerate each full Mat row once; scheduling across levels may differ in RTL."""
    if type(width) is not int or not 1 <= width <= 1280:
        raise ValueError("width 1..1280 required")
    if type(height) is not int or not 1 <= height <= 720:
        raise ValueError("height 1..720 required")
    if type(base) is not int or not 0 <= base < 1 << 32 or base % 16:
        raise ValueError("16-byte aligned 32-bit base required")
    layout = tracking_pyramid_layout(width, height)
    if base + layout["output_frame_bytes"] > 1 << 32:
        raise ValueError("frame exceeds 32-bit DMA range")
    rows, padding = [], []
    planes = layout["planes"]
    for index in range(0, len(planes), 2):
        gray, gradient = planes[index:index + 2]
        h = gray["height"]
        border = tuple(range(HALO)) + tuple(range(h + HALO, h + 2 * HALO))
        for y in range(h):
            destinations = (y + HALO,) + tuple(d for d in border if reflect101(d - HALO, h) == y)
            for destination in destinations:
                rows.append(MatRowDestination(gray["level"], y, destination,
                    base + gray["offset"] + destination * gray["row_bytes"],
                    base + gradient["offset"] + destination * gradient["row_bytes"],
                    gray["row_bytes"], gradient["row_bytes"], destination != y + HALO))
    for index, plane in enumerate(planes):
        end = plane["offset"] + plane["bytes"]
        stop = planes[index + 1]["offset"] if index + 1 < len(planes) else layout["output_frame_bytes"]
        if stop > end:
            padding.append((base + end, stop - end))
    return MatFrameWritePlan(tuple(rows), tuple(padding), layout["output_frame_bytes"])
