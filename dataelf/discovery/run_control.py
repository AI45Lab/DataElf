"""Per-run lifecycle and process ownership, shared by every entrypoint."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import Callable, Iterator

from dataelf.schemas import now_utc


class RunCancelled(RuntimeError):
    pass


def terminate_process(process: subprocess.Popen, *, grace: float = 2) -> None:
    try:
        if os.name == "posix":
            # The leader can exit before its descendants. Its process group is
            # still owned by this run and must be reaped on cancellation.
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait(timeout=5)


@dataclass
class RunControl:
    job_id: str | None = None
    workspace_path: Path | None = None
    on_stage: Callable[[str], None] | None = None
    stage: str = "initialization"
    secrets: set[str] = field(default_factory=set, repr=False)
    _cancelled: Event = field(default_factory=Event, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)
    _processes: set = field(default_factory=set, repr=False)

    def check(self) -> None:
        if self._cancelled.is_set():
            raise RunCancelled("Run cancelled.")

    def emit(self, stage: str) -> None:
        self.stage = stage
        if self.workspace_path is not None:
            path = self.workspace_path / "logs" / "run_events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"stage": stage, "at": now_utc().isoformat()}) + "\n")
        if self.on_stage:
            self.on_stage(stage)

    def record(self, event: str, payload: dict) -> None:
        if self.workspace_path is not None:
            from dataelf.discovery.redaction import redact_data
            path = self.workspace_path / "logs" / "stage_results.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": event, "payload": redact_data(payload, self.secrets)}, ensure_ascii=False) + "\n")

    def register(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.add(process)
            cancelled = self._cancelled.is_set()
        if cancelled:
            terminate_process(process)
            self.check()

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            terminate_process(process)


_CURRENT: ContextVar[RunControl | None] = ContextVar("dataelf_run_control", default=None)


def current_run() -> RunControl | None:
    return _CURRENT.get()


def check_cancelled() -> None:
    control = current_run()
    if control:
        control.check()


@contextmanager
def activate_run(control: RunControl) -> Iterator[None]:
    token = _CURRENT.set(control)
    try:
        yield
    finally:
        _CURRENT.reset(token)
