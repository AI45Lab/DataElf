"""Workspace file/code trio: execute_code, list_files, get_field_description.

execute_code runs agent Python in the finance code root; when linked sources
are in play (or DATAELF_FINANCE_READONLY_CODE forces it) the code runs in the
readonly_exec.py guarded subprocess instead of a bare ``python -c``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .limits import CODE_STDERR_CHARS_MAX, CODE_STDOUT_CHARS_MAX, LIST_FILES_DEFAULT_LIMIT, LIST_FILES_HARD_LIMIT
from .workspace import bounded_int, code_root, safe_relative


def _tail_clip(value: str, limit: int) -> tuple[str, bool]:
    """Keep the last `limit` characters; report whether anything was dropped."""
    if len(value) <= limit:
        return value, False
    return value[-limit:], True


def _readonly_code() -> bool:
    """Whether execute_code runs under read-only enforcement.

    Explicit DATAELF_FINANCE_READONLY_CODE wins ("1"/"true"/"yes"/"on" for
    on, anything else non-empty for off); otherwise enforcement is automatic
    whenever linked (symlinked) sources are present, because a stray write
    would corrupt the shared corpus instead of a workspace copy.
    """
    explicit = os.environ.get("DATAELF_FINANCE_READONLY_CODE", "")
    if explicit:
        return explicit.strip().lower() in ("1", "true", "yes", "on")
    return bool(os.environ.get("DATAELF_FINANCE_PROTECTED"))


def list_files(args: dict[str, Any]) -> dict[str, Any]:
    root = code_root()
    relative = safe_relative(args.get("path", "."))
    # Lexical join: safe_relative already rejects absolute paths and
    # ".." escapes, and resolving here would push symlinked inputs
    # (link: true sources) outside the root and reject them.
    base = root / relative
    if not base.exists():
        raise ValueError("path is outside the authorized finance workspace")
    pattern = args.get("pattern")
    recursive = bool(args.get("recursive", False))
    limit = bounded_int(args.get("limit"), 1, LIST_FILES_HARD_LIMIT, LIST_FILES_DEFAULT_LIMIT)
    iterator = base.rglob(pattern or "*") if recursive else base.glob(pattern or "*")
    files = [str(path.relative_to(root)) for path in sorted(iterator) if path.is_file()]
    returned = files[:limit]
    return {
        "base_path": str(root),
        "files": returned,
        "returned": len(returned),
        "truncated": len(files) > len(returned),
        "total_matches": len(files),
        "result_limit": {"unit": "items", "limit": limit},
    }


def get_field_description(args: dict[str, Any]) -> dict[str, Any]:
    path = safe_relative(args.get("data_file", ""))
    # Lexical join, same rationale as list_files: symlinked inputs must
    # stay addressable while safe_relative blocks ".." escapes.
    candidate = code_root() / path
    if not candidate.is_file():
        raise ValueError("data_file is outside the authorized finance workspace")
    if candidate.suffix == ".json":
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        return {"data_file": str(path), "fields": sorted(payload.keys()) if isinstance(payload, dict) else []}
    if candidate.suffix in {".csv", ".tsv"}:
        import csv
        with candidate.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if candidate.suffix == ".tsv" else ",")
            return {"data_file": str(path), "fields": next(reader, [])}
    return {"data_file": str(path), "fields": []}


def execute_code(args: dict[str, Any]) -> dict[str, Any]:
    code = str(args.get("code", ""))
    if not code.strip():
        raise ValueError("code is required")
    timeout = max(1, min(int(args.get("timeout", 30)), 300))
    if _readonly_code():
        # Wraps the code with write protection for linked sources; see
        # readonly_exec.py in this package. Same process count as the
        # plain path.
        completed = subprocess.run(
            [sys.executable, "-m", "dataelf.domains.finance.tools.readonly_exec"],
            cwd=code_root(), capture_output=True, text=True, timeout=timeout, input=code,
        )
    else:
        completed = subprocess.run([sys.executable, "-c", code], cwd=code_root(), capture_output=True, text=True, timeout=timeout)
    stdout, stdout_truncated = _tail_clip(completed.stdout, CODE_STDOUT_CHARS_MAX)
    stderr, stderr_truncated = _tail_clip(completed.stderr, CODE_STDERR_CHARS_MAX)
    return {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "returncode": completed.returncode,
    }
