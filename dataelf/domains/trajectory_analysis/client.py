"""Agent-facing WT access with evidence persisted at the actual call boundary."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .connector import (
    RAW,
    EvidenceError,
    _write,
    append_call,
    capture_lock,
    envelope_result,
    load_capture,
    persist_capture,
    read_json,
    require,
    validate_arguments,
    validate_sequence,
)


class TrajectoryClient:
    def __init__(self, workspace: Path, python: str, search_filters: dict | None = None):
        self.workspace = workspace
        self.python = python
        self._search_filters = dict(search_filters or {})

    @classmethod
    def from_env(cls) -> TrajectoryClient:
        require(os.environ.get("DATAELF_DOMAIN") == "trajectory_analysis"
                and os.environ.get("DATAELF_TRAJECTORY_CAPTURE") == "1",
                "QUERY_CLIENT_NOT_ENABLED")
        try:
            workspace = Path(os.environ["DATAELF_JOB_WORKSPACE"]).resolve()
            require(read_json(workspace, "job_spec.json")["domain"] == "trajectory_analysis",
                    "QUERY_WORKSPACE_INVALID")
            python = os.environ["DATAELF_TRAJECTORY_TOOL_PYTHON"]
            require(Path(python).is_absolute(), "QUERY_PYTHON_INVALID")
        except (OSError, KeyError, ValueError):
            raise EvidenceError("QUERY_CLIENT_ENV_INVALID") from None
        parameters = read_json(workspace, "job_spec.json").get("parameters", {})
        filters = {key: parameters[key] for key in ("job_id", "session_id")
                   if key in parameters}
        return cls(workspace, python, search_filters=filters)

    def search_records(self, *, reward: float = 0, limit: int = 1,
                       job_id: str | None = None, session_id: str | None = None):
        arguments = {"reward": reward, "limit": limit}
        for key, explicit in (("job_id", job_id), ("session_id", session_id)):
            fixed = self._search_filters.get(key)
            require(fixed is None or explicit is None or explicit == fixed,
                    "QUERY_SEARCH_FILTER_MISMATCH")
            value = fixed if fixed is not None else explicit
            if value is not None:
                arguments[key] = value
        return self._call("wt_search_records", arguments)

    def get_record(self, record_id: str, *, fields: list[str] | None = None):
        return self._call("wt_get_record", {"record_id": record_id,
                                           "fields": ["chosen_trace"] if fields is None else fields})

    def _call(self, tool: str, arguments: dict):
        # Reject unsupported domain calls before execution, including credential arguments.
        validate_arguments(tool, arguments)
        with capture_lock(self.workspace):
            raw = load_capture(self.workspace)
            if tool == 'wt_get_record' and raw['calls']:
                data = envelope_result(raw['calls'][0]['envelope'], raw['calls'][0]['transport'])
                if data and data['records'] and data['records'][0].get('job_id') is not None:
                    arguments['job_id'] = data['records'][0]['job_id']
            # Reserve durably before transport. A killed process remains a counted failed
            # attempt and stops acquisition; new processes cannot reset the budget.
            append_call(raw, tool, arguments, None, 'error')
            try:
                persist_capture(self.workspace, raw)
            except Exception:
                try:
                    raw['capture_failed'] = True
                    _write(self.workspace, RAW, raw)
                except Exception:
                    pass
                raise EvidenceError('QUERY_CAPTURE_FAILED') from None
            return self._execute(tool, arguments, raw)

    def _execute(self, tool, arguments, raw):
        envelope, transport = None, "error"
        try:
            result = subprocess.run(
                [self.python, "-B", "-m",
                 "dataelf.domains.trajectory_analysis.tools.wt_serving.bridge"],
                input=json.dumps({"tool": tool, "arguments": arguments}).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
                cwd=self.workspace,
            )
            if result.returncode == 0 and len(result.stdout) <= 64 * 1024 + 128:
                candidate = json.loads(result.stdout)
                envelope_result(candidate, "ok")
                envelope, transport = candidate, "ok"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # Never expose process/SDK/credential exceptions.
        try:
            raw['calls'][-1].update(envelope=envelope, transport=transport)
            # Persist actual response even when its locator violates the contract.
            persist_capture(self.workspace, raw)
            validate_sequence(raw['calls'])
        except Exception:
            # A failed later recording must not leave earlier evidence looking complete.
            try:
                raw = read_json(self.workspace, RAW)
                raw["capture_failed"] = True
                _write(self.workspace, RAW, raw)
            except Exception:
                pass  # Missing/unwritable required evidence also fails core validation.
            raise EvidenceError("QUERY_CAPTURE_FAILED") from None
        return envelope if transport == "ok" else {
            "isError": True, "result": {"error": "WT_READ_FAILED"}}
