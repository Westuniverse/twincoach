"""Deterministic MMIO oracle; not a cycle model or hardware transport."""
from __future__ import annotations

SHARED_POSE_ID = 0x50505332


def reference_config_write(address: int, data: int, strobe: int, *, busy: bool,
                           operator: int | None) -> tuple[int, int | None]:
    """Return (AXI response, resulting operator); rejected writes change nothing."""
    accepted = address == 8 and strobe == 15 and data in (0, 1, 2, 3) and not busy
    return (0, data) if accepted else (2, operator)


def reference_config_read(address: int, *, busy: bool,
                          operator: int | None) -> tuple[int, int]:
    """Read snapshots have no clear-on-read side effects."""
    if address == 0:
        return 0, SHARED_POSE_ID
    if address == 4:
        return 0, int(busy) | (int(operator is not None) << 1)
    if address == 8:
        return 0, 7 if operator is None else operator
    return 2, 0
