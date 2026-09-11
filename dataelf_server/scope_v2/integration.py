from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataelf_server.scope_v2.client import ScopeV2AIIndexClient
from dataelf_server.scope_v2.contracts import ScopePlan
from dataelf_server.scope_v2.runner import ScopeV2Executor


class ScopeV2IntegrationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ScopeV2PrefetchResult:
    execution: dict[str, Any]
    result_path: Path
    trace_ids: list[str]
    item_count: int

    @property
    def source_trace_id(self) -> str | None:
        return self.trace_ids[-1] if self.trace_ids else None


def prefetch_scope_v2(
    plan: ScopePlan,
    workspace_path: Path,
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 30,
) -> ScopeV2PrefetchResult:
    """Execute one scope plan inside a DataElf job workspace."""

    client = ScopeV2AIIndexClient(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    execution = ScopeV2Executor(
        client=client,
        output_root=workspace_path / "scope_v2",
    ).execute(plan)
    result_path = Path(str(execution["artifact_dir"])) / "result.json"
    trace_ids: list[str] = []
    item_count = 0
    for source in execution.get("sources", {}).values():
        if not isinstance(source, dict):
            continue
        item_count += int(source.get("kept_count") or 0)
        trace_ids.extend(
            str(value)
            for value in source.get("trace_ids", [])
            if value not in (None, "")
        )
    if item_count <= 0:
        raise ScopeV2IntegrationError(
            "no_source_data",
            "Scope V2 completed successfully but no records remained in the requested time window.",
        )
    return ScopeV2PrefetchResult(
        execution=execution,
        result_path=result_path.resolve(),
        trace_ids=trace_ids,
        item_count=item_count,
    )


def materialize_filtered_ai_index_envelopes(
    plan: ScopePlan,
    prefetch: ScopeV2PrefetchResult,
    workspace_path: Path,
) -> list[Path]:
    """Expose filtered Scope V2 rows through DataElf's normal raw envelope contract.

    Original page responses remain under scope_v2; these raw envelopes contain
    the filtered view consumed by server modeling and presentation.
    """

    raw_dir = workspace_path / "raw" / "ai_index"
    raw_dir.mkdir(parents=True, exist_ok=True)
    calls = {call.source: call for call in plan.calls}
    written: list[Path] = []
    for source_name, source in prefetch.execution.get("sources", {}).items():
        if not isinstance(source, dict):
            continue
        rows = [
            item["data"]
            for item in source.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("data"), dict)
        ]
        if not rows:
            continue
        call = calls[source_name]
        trace_ids = [str(value) for value in source.get("trace_ids", []) if value]
        data = {"total": len(rows), "list": rows}
        envelope = {
            "source": "ai_index",
            "mode": "api",
            "method": call.method,
            "endpoint": call.endpoint,
            "request": {
                **call.payload,
                "page_size": call.page_size,
                "max_pages": call.max_pages,
                "window": {
                    "start": plan.window.start,
                    "end": plan.window.end,
                    "timezone": plan.timezone,
                },
            },
            "trace_id": trace_ids[-1] if trace_ids else None,
            "trace_ids": trace_ids,
            "data": data,
            "raw": {
                "scope_v2_result": str(prefetch.result_path),
                "raw_files": source.get("raw_files", []),
            },
        }
        path = raw_dir / f"scope_v2_{source_name}.json"
        _write_json_atomic(path, envelope)
        written.append(path.resolve())
    if not written:
        raise ScopeV2IntegrationError(
            "no_source_data",
            "Scope V2 produced no non-empty source envelopes for Pi.",
        )
    return written


def _write_json_atomic(path: Path, payload: Any) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)
