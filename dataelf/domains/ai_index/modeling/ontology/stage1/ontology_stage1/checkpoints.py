from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import secrets
import sys
from contextlib import AbstractContextManager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, file_sha256, read_json_object, sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config


CHECKPOINT_VERSION = 2
TERMINAL_STAGES = {"completed", "terminal_failed"}
RESUMABLE_STAGES = {
    "source_profiled",
    "generator_running",
    "candidate_staged",
    "validated",
    "reviewer_running",
    "reviewed",
    "repair_pending",
    "paused_timeout",
    "paused_interrupted",
    "paused_runtime_error",
    "awaiting_manual_audit",
}
_ARTIFACT_SUBDIR: ContextVar[str] = ContextVar("ontology_stage1_artifact_subdir", default="ontology/stage1")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)


def stage1_root(workspace: Path, config: Stage1Config | None = None) -> Path:
    subdir = config.artifacts.subdir if config is not None else _ARTIFACT_SUBDIR.get()
    return workspace / Path(subdir)


def configure_artifact_subdir(config: Stage1Config) -> None:
    _ARTIFACT_SUBDIR.set(config.artifacts.subdir)


def checkpoint_path(workspace: Path, run_id: str) -> Path:
    return stage1_root(workspace) / ".checkpoints" / "runs" / run_id / "pipeline.json"


def candidate_root(workspace: Path, run_id: str) -> Path:
    return stage1_root(workspace) / "candidates" / run_id


def published_root(workspace: Path, run_id: str) -> Path:
    return stage1_root(workspace) / "published" / run_id


def log_path(workspace: Path, run_id: str) -> Path:
    return stage1_root(workspace) / "run_logs" / f"{run_id}.jsonl"


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    return value


def config_fingerprint(config: Stage1Config) -> str:
    value = _jsonable(config)
    value.pop("path", None)
    return sha256_json(value)


def _tree_fingerprint(paths: list[Path], root: Path) -> str:
    files: list[dict[str, str]] = []
    for path in sorted(set(paths)):
        if path.is_file():
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = str(path)
            files.append({"path": relative, "sha256": file_sha256(path)})
    return sha256_json(files)


def implementation_fingerprint() -> str:
    ontology_root = Path(__file__).resolve().parents[2]
    files = [
        path
        for path in ontology_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ".ts", ".json"}
    ]
    return _tree_fingerprint(files, ontology_root)


def runtime_fingerprint() -> str:
    root = Path(__file__).resolve().parent.parent
    return _tree_fingerprint(list((root / "runtime").glob("*.ts")), root)


def compatibility_fingerprints(config: Stage1Config, source_fingerprint: str, prompt_fingerprint: str) -> dict[str, str]:
    return {
        "source": source_fingerprint,
        "config": config_fingerprint(config),
        "model": sha256_json({"generator": _jsonable(config.generator), "reviewer": _jsonable(config.reviewer)}),
        "prompt": prompt_fingerprint,
        "runtime": runtime_fingerprint(),
        "implementation": implementation_fingerprint(),
    }


def make_checkpoint(run_id: str, compatibility: dict[str, str], source_cache: Path) -> dict[str, Any]:
    now = utc_now()
    return {
        "checkpointVersion": CHECKPOINT_VERSION,
        "runId": run_id,
        "stage": "source_profiled",
        "createdAt": now,
        "updatedAt": now,
        "compatibility": compatibility,
        "sourceCache": str(source_cache),
        "round": 0,
        "history": [{"at": now, "stage": "source_profiled"}],
    }


def save_checkpoint(workspace: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = utc_now()
    atomic_write_json(checkpoint_path(workspace, str(state["runId"])), state)


def transition(workspace: Path, state: dict[str, Any], stage: str, **updates: Any) -> None:
    state.update(updates)
    state["stage"] = stage
    state.setdefault("history", []).append({"at": utc_now(), "stage": stage, **updates})
    save_checkpoint(workspace, state)


def load_checkpoint(workspace: Path, run_id: str) -> dict[str, Any]:
    state = read_json_object(checkpoint_path(workspace, run_id))
    if state.get("checkpointVersion") != CHECKPOINT_VERSION or state.get("runId") != run_id:
        raise ValueError(f"invalid checkpoint for run {run_id}")
    return state


def list_checkpoints(workspace: Path) -> list[dict[str, Any]]:
    root = stage1_root(workspace) / ".checkpoints" / "runs"
    result: list[dict[str, Any]] = []
    if not root.exists():
        return result
    for path in sorted(root.glob("*/pipeline.json"), reverse=True):
        try:
            result.append(read_json_object(path))
        except ValueError:
            continue
    return result


def compatibility_mismatches(state: dict[str, Any], expected: dict[str, str]) -> list[str]:
    actual = state.get("compatibility", {})
    return [key for key, value in expected.items() if actual.get(key) != value]


def select_resume(
    workspace: Path,
    resume: str | None,
    expected: dict[str, str],
) -> dict[str, Any] | None:
    if resume is None:
        return None
    if resume != "auto":
        state = load_checkpoint(workspace, resume)
        mismatches = compatibility_mismatches(state, expected)
        if mismatches:
            raise ValueError(
                f"run {resume} is incompatible in {', '.join(mismatches)}; use --repair-from to import its candidate"
            )
        if state.get("stage") not in RESUMABLE_STAGES:
            raise ValueError(f"run {resume} is not resumable from stage {state.get('stage')}")
        return state
    for state in list_checkpoints(workspace):
        if state.get("stage") in RESUMABLE_STAGES and not compatibility_mismatches(state, expected):
            return state
    return None


_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|bearer|password|secret|token)", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if _SECRET_KEY.search(str(key)) else redact(child)) for key, child in value.items()}
    if isinstance(value, list):
        return [redact(child) for child in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    return value


def append_event(workspace: Path, run_id: str, event: str, **payload: Any) -> None:
    target = log_path(workspace, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = redact({"at": utc_now(), "runId": run_id, "event": event, **payload})
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class RunLock(AbstractContextManager["RunLock"]):
    def __init__(self, workspace: Path, run_id: str) -> None:
        self.path = checkpoint_path(workspace, run_id).parent / "run.lock"
        self.handle: Any = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError(f"run is already locked: {self.path.parent.name}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} python={sys.executable}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return None
