"""Machine-readable identity for one Linux boot session."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID


LINUX_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def normalize_linux_boot_id(value: object) -> str | None:
    """Return one canonical non-zero UUID, or ``None`` for invalid input."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    if parsed.int == 0 or text != canonical:
        return None
    return canonical


def read_linux_boot_id(path: str | Path = LINUX_BOOT_ID_PATH) -> str | None:
    """Read Linux's current boot UUID without inventing a fallback identity."""

    try:
        raw = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    return normalize_linux_boot_id(raw)

