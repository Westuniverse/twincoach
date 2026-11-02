"""Shared command selection for native Linux and Windows-hosted WSL."""
from __future__ import annotations

import os
from pathlib import Path
import re


def uses_wsl() -> bool:
    return os.name == "nt"


def command_prefix(distribution: str) -> list[str]:
    if not uses_wsl():
        return []
    if not distribution.strip():
        raise ValueError("WSL distribution must not be empty")
    return ["wsl.exe", "--distribution", distribution.strip(), "--exec"]


def execution_path(path: Path) -> str:
    resolved = path.resolve()
    if not uses_wsl():
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    if len(drive) == 1 and drive.isalpha():
        return f"/mnt/{drive}/{resolved.as_posix()[3:]}"
    if not drive:
        return resolved.as_posix()
    raise ValueError(f"Unsupported WSL path: {resolved}")


def execution_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if uses_wsl():
        environment["PATH"] = ";".join(
            part for part in environment.get("PATH", "").split(";")
            if not re.match(r"^[dDeE]:", part)
        )
    return environment
