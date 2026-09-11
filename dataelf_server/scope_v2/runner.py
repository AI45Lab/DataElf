from __future__ import annotations

from dataelf.discovery.run_control import check_cancelled

import ast
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from dataelf_server.scope_v2.client import ScopeV2AIIndexClient, ScopeV2AIIndexError
from dataelf_server.scope_v2.contracts import ScopeCall, ScopePlan, TIMEZONE


SOURCE_TIME_PATHS: dict[str, tuple[str, ...]] = {
    "news": ("date",),
    "twitter": ("published_at",),
    "github": ("published_at",),
    "huggingface": ("published_at",),
    "youtube": ("video_info", "publishtime"),
}


class ScopeV2Executor:
    def __init__(
        self,
        *,
        client: ScopeV2AIIndexClient,
        output_root: Path,
    ):
        self.client = client
        self.output_root = output_root

    def execute(self, plan: ScopePlan, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or _new_run_id()
        run_dir = self.output_root / run_id
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=False)
        _write_json(run_dir / "plan.json", plan.to_dict())

        result: dict[str, Any] = {
            "version": plan.version,
            "status": "completed",
            "run_id": run_id,
            "artifact_dir": str(run_dir.resolve()),
            "plan": plan.to_dict(),
            "sources": {},
            "warnings": [],
        }
        try:
            for call in plan.calls:
                source_result = self._execute_call(
                    call,
                    plan=plan,
                    run_dir=run_dir,
                    raw_dir=raw_dir,
                    warnings=result["warnings"],
                )
                result["sources"][call.source] = source_result
        except ScopeV2AIIndexError as exc:
            failure = {
                "version": plan.version,
                "status": "failed",
                "run_id": run_id,
                "artifact_dir": str(run_dir.resolve()),
                "error": exc.to_dict(),
                "warnings": result["warnings"],
            }
            _write_json(run_dir / "error.json", failure)
            raise

        _write_json(run_dir / "result.json", result)
        return result

    def _execute_call(
        self,
        call: ScopeCall,
        *,
        plan: ScopePlan,
        run_dir: Path,
        raw_dir: Path,
        warnings: list[str],
    ) -> dict[str, Any]:
        if plan.time_mode not in {"date_range", "latest_on_or_before_date"}:
            raise ValueError("Unsupported acquisition time mode")
        return self._execute_date_bounded_call(
            call,
            plan=plan,
            run_dir=run_dir,
            raw_dir=raw_dir,
            warnings=warnings,
        )

    def _execute_date_bounded_call(
        self,
        call: ScopeCall,
        *,
        plan: ScopePlan,
        run_dir: Path,
        raw_dir: Path,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Acquire the requested interval; archived plans retain their old time mode.

        News sends the range directly to AI Index. Ecosystem endpoints have no
        date parameters, so they are scanned in descending time order with a
        bounded page count and filtered locally.
        """

        requested_end = datetime.fromisoformat(plan.window.end)
        requested_date = requested_end.date()
        window_start = datetime.fromisoformat(plan.window.start).date()
        trace_ids: list[str] = []
        raw_files: list[str] = []
        candidates: list[tuple[dict[str, Any], datetime, str]] = []
        pages_requested = 0
        total_reported: int | None = None
        exact_range = plan.time_mode == "date_range"
        scan_complete = call.source == "news" and not exact_range

        for page in range(1, call.max_pages + 1):
            check_cancelled()
            payload = {**call.payload, "page": page, "size": call.page_size}
            response = self.client.post(call.endpoint, payload)
            pages_requested += 1
            raw_path = raw_dir / f"{call.source}_range_page_{page}.json"
            _write_json(raw_path, response)
            raw_ref = raw_path.relative_to(run_dir).as_posix()
            raw_files.append(raw_ref)
            trace_id = _optional_string(response.get("trace_id"))
            if trace_id:
                trace_ids.append(trace_id)

            total = response["data"].get("total")
            if isinstance(total, int) and total_reported is None:
                total_reported = total
            rows = response["data"]["list"]
            reached_before_window = False
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict):
                    warnings.append(
                        f"{call.source} page {page} row {row_index + 1}: ignored non-object item"
                    )
                    continue
                published = _item_datetime(call.source, row)
                if published is None:
                    warnings.append(
                        f"{call.source} page {page} row {row_index + 1}: missing or invalid publication time"
                    )
                    continue
                if published.date() < window_start:
                    reached_before_window = True
                    continue
                if published > requested_end:
                    continue
                if _matches_retrieval(row, getattr(plan, "retrieval", {})):
                    candidates.append((row, published, raw_ref))

            if call.source == "news" and not exact_range:
                break
            exhausted = not rows or (
                total_reported is not None
                and page * call.page_size >= total_reported
            )
            if (reached_before_window and call.stop_when_older) or exhausted:
                scan_complete = True
                break

        if not scan_complete and pages_requested == call.max_pages:
            warnings.append(
                f"{call.source}: historical scan reached the {call.max_pages}-page limit"
            )

        if not candidates:
            warnings.append(
                f"{call.source}: no records found from {window_start.isoformat()} through {requested_date.isoformat()}"
            )
            return _empty_date_bounded_source(
                call,
                requested_date=requested_date,
                window_start=window_start,
                pages_requested=pages_requested,
                trace_ids=trace_ids,
                raw_files=raw_files,
                total_reported=total_reported,
                scan_complete=scan_complete,
            )

        effective_date = max(published for _, published, _ in candidates).date()
        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row, published, raw_ref in candidates:
            if not exact_range and published.date() != effective_date:
                continue
            normalized = _normalize_item(
                call.source,
                row,
                raw_ref=raw_ref,
                published=published,
            )
            if normalized["source_id"] in seen_ids:
                continue
            seen_ids.add(normalized["source_id"])
            items.append(normalized)

        return {
            "source": call.source,
            "endpoint": call.endpoint,
            "pages_requested": pages_requested,
            "probe_pages_requested": 0,
            "formal_pages_requested": pages_requested,
            "requested_date": requested_date.isoformat(),
            "window_start": window_start.isoformat(),
            "effective_date": effective_date.isoformat() if not exact_range or window_start == requested_date else None,
            "fallback_applied": not exact_range and effective_date != requested_date,
            "time_mode": plan.time_mode,
            "server_time_filter": call.source == "news",
            "scan_complete": scan_complete,
            "probe_trace_id": None,
            "total_reported": total_reported,
            "kept_count": len(items),
            "trace_ids": trace_ids,
            "raw_files": raw_files,
            "items": items,
        }


def _matches_retrieval(row: dict, retrieval: dict) -> bool:
    """Literal case-insensitive matching over returned records, before modeling.

    Each positive group is OR-matched; keyword and entity groups combine with AND.
    No undocumented API search parameters or inferred synonym expansion are used.
    """
    text = json.dumps(row, ensure_ascii=False).casefold()
    for group in ("keywords", "entities"):
        terms = retrieval.get(group) or []
        if terms and not any(term.casefold() in text for term in terms):
            return False
    return not any(term.casefold() in text for term in retrieval.get("exclude_keywords", []))


def _empty_date_bounded_source(
    call: ScopeCall,
    *,
    requested_date: Any,
    window_start: Any,
    pages_requested: int,
    trace_ids: list[str],
    raw_files: list[str],
    total_reported: int | None,
    scan_complete: bool,
) -> dict[str, Any]:
    return {
        "source": call.source,
        "endpoint": call.endpoint,
        "pages_requested": pages_requested,
        "probe_pages_requested": 0,
        "formal_pages_requested": pages_requested,
        "requested_date": requested_date.isoformat(),
        "window_start": window_start.isoformat(),
        "effective_date": None,
        "fallback_applied": False,
        "server_time_filter": call.source == "news",
        "scan_complete": scan_complete,
        "probe_trace_id": None,
        "total_reported": total_reported,
        "kept_count": 0,
        "trace_ids": trace_ids,
        "raw_files": raw_files,
        "items": [],
    }


def load_env_file(path: Path, *, override: bool = False) -> None:
    """Load the small KEY=VALUE subset used by the project's shell-compatible .env."""

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                value = str(ast.literal_eval(value))
            except (SyntaxError, ValueError):
                value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def parse_cli_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result["status"],
        "run_id": result["run_id"],
        "artifact_dir": result["artifact_dir"],
        "mode": result["plan"]["mode"],
        "window": result["plan"]["window"],
        "sources": {
            source: {
                "pages_requested": details["pages_requested"],
                "kept_count": details["kept_count"],
                "trace_ids": details["trace_ids"],
                "effective_date": details.get("effective_date"),
                "fallback_applied": details.get("fallback_applied", False),
            }
            for source, details in result["sources"].items()
        },
        "warning_count": len(result["warnings"]),
    }


def _item_datetime(source: str, item: dict[str, Any]) -> datetime | None:
    value: Any = item
    for key in SOURCE_TIME_PATHS[source]:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def _normalize_item(
    source: str,
    item: dict[str, Any],
    *,
    raw_ref: str,
    published: datetime | None,
) -> dict[str, Any]:
    source_id = _source_id(source, item)
    title, url = _title_and_url(source, item)
    return {
        "source": source,
        "source_id": source_id,
        "title": title,
        "published_at": published.isoformat() if published else None,
        "url": url,
        "raw_ref": raw_ref,
        "data": item,
    }


def _source_id(source: str, item: dict[str, Any]) -> str:
    candidates = {
        "news": item.get("news_id"),
        "twitter": item.get("tweet_post_id"),
        "youtube": item.get("video_id"),
        "github": item.get("url") or item.get("title"),
        "huggingface": item.get("url") or item.get("title"),
    }
    value = candidates[source]
    if value not in (None, ""):
        return f"{source}:{value}"
    digest = hashlib.sha256(
        json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{source}:{digest}"


def _title_and_url(source: str, item: dict[str, Any]) -> tuple[str | None, str | None]:
    if source == "youtube":
        info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
        return _optional_string(info.get("title")), _optional_string(info.get("url"))
    if source == "twitter":
        text = _optional_string(item.get("text"))
        title = text[:160] if text else None
        return title, _optional_string(item.get("url"))
    return _optional_string(item.get("title")), _optional_string(item.get("url"))


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _new_run_id() -> str:
    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%dT%H%M%S")
    return f"run_{timestamp}_{uuid.uuid4().hex[:8]}"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
