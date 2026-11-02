# SPDX-License-Identifier: GPL-2.0-only
"""Assign logical scoring roles without changing physical pose consumers."""
from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Mapping

from .contracts import CameraView, PoseFrame


class ScorerViewAdapter:
    """Session-fixed push sink; only the scorer's immutable view tag changes."""

    def __init__(
        self, sink, physical_to_logical: Mapping[str, str] | None = None,
    ) -> None:
        mapping = ({view: view for view in CameraView} if physical_to_logical is None else
                   {CameraView(p): CameraView(v) for p, v in physical_to_logical.items()})
        if set(mapping) != set(CameraView) or set(mapping.values()) != set(CameraView):
            raise ValueError(
                "scoring view mapping must biject physical front/side to logical front/side"
            )
        self.sink = sink
        self.physical_to_logical = MappingProxyType(mapping)
        self.is_identity = all(physical is logical for physical, logical in mapping.items())

    @property
    def metadata(self) -> dict:
        mapping = {p.value: v.value for p, v in self.physical_to_logical.items()}
        return {"physical_to_logical": mapping,
                "logical_to_physical": {v: p for p, v in mapping.items()},
                "raw17_key_space": "physical", "scorer_view_tag_space": "logical"}

    def adapt(self, physical_frame: PoseFrame) -> PoseFrame:
        logical = self.physical_to_logical[physical_frame.view]
        return (physical_frame if logical is physical_frame.view else
                replace(physical_frame, view=logical))

    def push(self, physical_frame: PoseFrame):
        return self.sink.push(self.adapt(physical_frame))


def map_scoring_views(sink, physical_to_logical: Mapping[str, str] | None = None):
    """Return the original sink for identity; otherwise install one role boundary."""
    adapter = ScorerViewAdapter(sink, physical_to_logical)
    return sink if adapter.is_identity else adapter
