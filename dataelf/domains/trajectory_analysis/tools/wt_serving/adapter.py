"""The only WT query boundary. Never accept table names or query text from tools."""
from __future__ import annotations

import json
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr

MAX_LIMIT = 20
MAX_OUTPUT_BYTES = 64 * 1024
TrajectoryField = Literal["messages", "response", "chosen_trace", "rejected_trace", "meta_json"]
TRAJECTORY_COLUMNS: tuple[TrajectoryField, ...] = ("messages", "response", "chosen_trace", "rejected_trace", "meta_json")
COLUMNS = (
    "id", "job_id", "session_id", "step_id", "created_at", "source_updated_at",
    "serving_updated_at", "is_terminal", "is_truncated", "is_session_completed",
    "is_trainable", "step_reward", "reward",
)
IDENTIFIER = re.compile(r"[^\x00-\x1f\x7f]{1,1024}\Z")


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Search(Arguments):
    reward: StrictInt | StrictFloat | None = Field(default=None, allow_inf_nan=False)
    job_id: StrictStr | None = None
    session_id: StrictStr | None = None
    limit: StrictInt = Field(default=5, ge=1, le=MAX_LIMIT)


class Get(Arguments):
    record_id: StrictStr
    job_id: StrictStr | None = None
    fields: list[TrajectoryField] = Field(
        default_factory=lambda: list(TRAJECTORY_COLUMNS), min_length=1, max_length=5,
    )


MODELS: dict[str, type[Arguments]] = {"wt_search_records": Search, "wt_get_record": Get}


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def validate(tool: str, arguments: Any) -> Arguments:
    request = MODELS[tool].model_validate(arguments)
    for key, value in request.model_dump().items():
        if key in {"record_id", "job_id", "session_id"} and value is not None:
            identifier(value)
    return request


class QueryClient(Protocol):
    config: Any

    def query_data(self, **kwargs: Any) -> list[dict[str, Any]]: ...


def json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


def bounded_result(rows: list[dict[str, Any]], limit: int, columns: tuple[str, ...]) -> dict[str, Any]:
    # Preserve SDK values and absent/null distinctions. Omit entire fields only
    # when necessary; never rewrite or slice a WT message/trace into a new format.
    records = [{key: row[key] for key in columns if key in row} for row in rows[:limit]]
    result: dict[str, Any] = {
        "records": records, "limit": limit, "truncated": len(rows) > limit,
        "omitted_fields": [], "omitted_records": max(0, len(rows) - limit),
    }
    while json_bytes(result) > MAX_OUTPUT_BYTES:
        candidates = [(json_bytes(record[key]), index, key)
                      for index, record in enumerate(records)
                      for key in TRAJECTORY_COLUMNS if key in record]
        if candidates:
            _, index, key = max(candidates)
            del records[index][key]
            result["omitted_fields"].append({"record_index": index, "field": key})
        elif records:
            records.pop()
            result["omitted_records"] += 1
        else:
            raise ValueError("output budget exceeded")
        result["truncated"] = True
    return result


class ServingAdapter:
    def __init__(self, client: QueryClient):
        self._client = client
        self._check_table()

    def _check_table(self) -> None:
        tables = self._client.config.tables
        if tables.profile != "test" or tables.serving_table != "serving_test":
            raise ValueError("WT test serving configuration required")

    def call(self, tool: str, arguments: Any) -> dict[str, Any]:
        request = validate(tool, arguments)
        self._check_table()
        predicates = []
        for field in ("job_id", "session_id"):
            value = getattr(request, field, None)
            if value is not None:
                predicates.append(f"{field} = '{identifier(value).replace(chr(39), chr(39) * 2)}'")
        limit = getattr(request, "limit", 1)
        if isinstance(request, Get):
            predicates.append(f"id = '{identifier(request.record_id).replace(chr(39), chr(39) * 2)}'")
        if isinstance(request, Search) and request.reward is not None:
            predicates.append(f"reward = {request.reward!r}")
        columns: tuple[str, ...] = COLUMNS
        if isinstance(request, Get):
            columns += tuple(field for field in TRAJECTORY_COLUMNS if field in request.fields)
        config = self._client.config
        rows = self._client.query_data(
            table=config.tables.serving_table,
            filter_query=" AND ".join(predicates) or "id IS NOT NULL",
            limit=limit, columns=list(columns),
            order_by=None,
            ascending=True, checkout_latest=True, exclude_none=False,
            deserialize_json=True,
        )
        return bounded_result(rows, limit, columns)


def create_client() -> Any:
    """Called only inside the bridge's muted SDK process boundary."""
    import os

    from wt_sdk.client import WTGatewayClient
    from wt_sdk.config import GatewayConfig

    required = ("WT_SDK_DB_URI", "WT_SDK_S3_ENDPOINT", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY")
    if os.environ.get("WT_SDK_PROFILE") != "test" or not all(os.getenv(k) for k in required):
        raise ValueError("WT test environment required")
    config = GatewayConfig()
    if config.tables.profile != "test" or config.tables.serving_table != "serving_test":
        raise ValueError("WT test serving configuration required")
    config.log_dldb_metrics_summary_on_close = False
    # DLDB normally creates a missing information_schema during connect.
    # Replace only that constructor; never invoke the auto-creating path.
    from unittest.mock import patch

    from .catalog import ServingCatalog

    with patch("dldb.session.InformationSchemaTable", ServingCatalog):
        return WTGatewayClient(config=config)
