"""Shared workspace paths and argument guards for the finance tools.

Env-resolved roots (DATAELF_FINANCE_CODE_ROOT / DATAELF_WORKSPACE), the
storage-key namespace shared by parse_html_page and retrieve_information,
and the small cross-family argument validations (workspace-relative paths,
bounded integers).
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any


def code_root() -> Path:
    value = os.environ.get("DATAELF_FINANCE_CODE_ROOT") or os.environ.get("DATAELF_WORKSPACE")
    if not value:
        raise ValueError("Finance code root is not configured")
    return Path(value).resolve()


def workspace_root() -> Path:
    value = os.environ.get("DATAELF_WORKSPACE")
    if not value:
        raise ValueError("Finance workspace is not configured")
    return Path(value).resolve()


def storage_path(key: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key):
        raise ValueError("key must contain only letters, numbers, '.', '_' or '-' and be at most 128 characters")
    directory = workspace_root() / "raw" / "finance" / "storage"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{key}.txt"


def safe_relative(value: Any) -> Path:
    path = Path(str(value or "."))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be workspace-relative")
    return path


def bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"expected a number, got: {value}") from None
    if not math.isfinite(number):
        raise ValueError(f"expected a number, got: {value}") from None
    return max(minimum, min(int(number), maximum))
