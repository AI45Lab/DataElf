"""Read-only enforcement wrapper for the finance execute_code tool.

Activated by tools.py whenever linked (symlinked) sources are in play, or by
an explicit DATAELF_FINANCE_READONLY_CODE=1. Agent code is read from stdin
and executed with two guards installed:

* a ``sys.audithook`` refusing write-mode opens, renames/removes, and chmods
  that target the protected corpora (DATAELF_FINANCE_PROTECTED,
  ``os.pathsep``-separated real paths), and refusing subprocess/exec
  escapes (a child interpreter would not carry the hook);
* a ``sqlite3.connect`` wrapper rewriting protected database paths to
  read-only URIs, so ``pd.read_sql`` keeps working while a stray ``to_sql``
  fails with "attempt to write a readonly database" instead of corrupting
  the shared original (SQLite opens files in C, invisible to the hook).

Writes to unprotected paths (workspace scratch output) remain allowed: the
tool contract is read-only *analysis*, not a sandbox of the whole machine.

This is accident-proofing for model-driven code, not a security boundary --
raw syscalls via ctypes/mmap are not intercepted.
"""

from __future__ import annotations

import builtins
import os
import re
import sqlite3
import sys
import traceback
import urllib.parse

PROTECTED = [item for item in os.environ.get("DATAELF_FINANCE_PROTECTED", "").split(os.pathsep) if item]

_WRITE_FLAGS = (
    getattr(os, "O_WRONLY", 1)
    | getattr(os, "O_RDWR", 2)
    | getattr(os, "O_CREAT", 0)
    | getattr(os, "O_TRUNC", 0)
    | getattr(os, "O_APPEND", 0)
)
_MUTATING_EVENTS = frozenset({
    "os.remove", "os.rename", "os.replace", "os.rmdir", "os.truncate",
    "os.chmod", "os.chown", "os.link", "os.symlink",
    "shutil.copyfile", "shutil.rmtree",
})
_PROCESS_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "pty.spawn"})
_MODE_QUERY = re.compile(r"mode=[a-z]+")


class ReadOnlyViolation(PermissionError):
    """Raised into agent code when a write targets a protected source."""


def _is_protected(path: object) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        real = os.path.realpath(os.fsdecode(path))
    except (TypeError, ValueError, OSError):
        return False
    return any(real == root or real.startswith(root + os.sep) for root in PROTECTED)


def _audit(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writing = (isinstance(flags, int) and flags & _WRITE_FLAGS) or (
            isinstance(mode, str) and any(flag in mode for flag in "wax+")
        )
        if writing and _is_protected(path):
            raise ReadOnlyViolation(f"read-only enforcement: refusing write to linked finance source {os.fsdecode(path)!r}")
    elif event in _PROCESS_EVENTS:
        raise ReadOnlyViolation(f"read-only enforcement: {event} is disabled for analysis code")
    elif event in _MUTATING_EVENTS and any(_is_protected(item) for item in args):
        raise ReadOnlyViolation(f"read-only enforcement: refusing to mutate linked finance source via {event}")


_real_sqlite_connect = sqlite3.connect


def _readonly_sqlite_connect(database=None, *args, **kwargs):
    target = database if isinstance(database, (str, os.PathLike)) else None
    if target:
        text = os.fsdecode(target)
        if not (text == ":memory:" or text.startswith("file::memory:") or "mode=memory" in text):
            if kwargs.get("uri") and text.startswith("file:"):
                raw_path, _, query = text[5:].partition("?")
                if _is_protected(urllib.parse.unquote(raw_path)):
                    if _MODE_QUERY.search(query):
                        query = _MODE_QUERY.sub("mode=ro", query)
                    else:
                        query = (query + "&") if query else ""
                        query += "mode=ro"
                    database = f"file:{raw_path}?{query}"
            elif _is_protected(text):
                database = "file:" + urllib.parse.quote(os.path.realpath(text)) + "?mode=ro"
                kwargs["uri"] = True
    return _real_sqlite_connect(database, *args, **kwargs)


def main() -> int:
    code = sys.stdin.read()
    sys.addaudithook(_audit)
    sqlite3.connect = _readonly_sqlite_connect
    try:
        exec(compile(code, "<execute_code>", "exec"), {"__name__": "__main__", "__builtins__": builtins})
    except BaseException:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
