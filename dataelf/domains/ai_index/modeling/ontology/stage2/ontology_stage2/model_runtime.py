from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, read_json_object
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import redact
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import ModelConfig, Stage2Config
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import Stage1Contract
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.prompts import COMPILER_SYSTEM, REVIEWER_SYSTEM


class ModelRuntimeError(RuntimeError):
    pass


_STAGE_DEADLINE: ContextVar[float | None] = ContextVar("stage2_deadline", default=None)


@contextmanager
def stage_deadline(total_seconds: int):
    token = _STAGE_DEADLINE.set(time.monotonic() + total_seconds)
    try:
        yield
    finally:
        _STAGE_DEADLINE.reset(token)


def ensure_stage_time_remaining(label: str = "Stage 2") -> float | None:
    deadline = _STAGE_DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelRuntimeError(f"{label} total stage timeout exhausted")
    return remaining


def _append_model_event(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def _request_heartbeat(path: Path | None, base_event: dict[str, Any]):
    started = time.monotonic()
    stopped = threading.Event()

    def emit() -> None:
        while not stopped.wait(30):
            _append_model_event(
                path,
                {
                    **base_event,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "type": "request_heartbeat",
                    "elapsedSeconds": round(time.monotonic() - started, 3),
                },
            )

    thread = threading.Thread(target=emit, name="stage2-model-heartbeat", daemon=True)
    thread.start()
    try:
        yield started
    finally:
        stopped.set()
        thread.join(timeout=1)


def endpoint_metadata(base_url: str) -> dict[str, Any]:
    parts = urlsplit(base_url)
    hostname = parts.hostname or ""
    port = parts.port
    netloc = hostname if port is None else f"{hostname}:{port}"
    return {
        "scheme": parts.scheme,
        "host": hostname,
        "port": port,
        "baseUrl": urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", "")),
    }

def _direct_json_request(
    *,
    model: ModelConfig,
    base_url: str,
    api_key: str,
    system_prompt: str,
    prompt: str,
    schema: dict[str, Any],
    label: str,
    event_log_path: Path | None = None,
) -> dict[str, Any]:
    body = {
        "model": model.name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": model.max_tokens,
        "temperature": model.temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "stage2_result", "strict": True, "schema": schema},
        },
    }
    if model.name.strip().lower().startswith("glm-"):
        body["thinking"] = {"type": "disabled"}
        body["chat_template_kwargs"] = {"enable_thinking": False}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    last_error: Exception | None = None
    envelope: dict[str, Any] | None = None
    for attempt in range(model.request_max_retries + 1):
        remaining = ensure_stage_time_remaining(label)
        request_timeout = float(model.request_timeout_seconds)
        if remaining is not None:
            request_timeout = min(request_timeout, remaining)
        event_base = {
            "attempt": attempt,
            "attempts": model.request_max_retries + 1,
            "label": label,
            "model": model.name,
            "endpoint": endpoint_metadata(base_url),
            "requestTimeoutSeconds": request_timeout,
        }
        _append_model_event(
            event_log_path,
            {**event_base, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "type": "request_start"},
        )
        with _request_heartbeat(event_log_path, event_base) as started:
            try:
                with urllib.request.urlopen(request, timeout=max(1.0, request_timeout)) as response:
                    envelope = json.loads(response.read())
                    status = getattr(response, "status", 200)
                _append_model_event(
                    event_log_path,
                    {
                        **event_base,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "type": "request_complete",
                        "status": status,
                        "elapsedSeconds": round(time.monotonic() - started, 3),
                        "usage": envelope.get("usage"),
                        "finishReason": ((envelope.get("choices") or [{}])[0].get("finish_reason")),
                    },
                )
                break
            except urllib.error.HTTPError as exc:
                body_text = redact(exc.read().decode(errors="replace"))
                last_error = ModelRuntimeError(f"{label} direct JSON request failed with HTTP {exc.code}: {body_text[:2000]}")
                _append_model_event(
                    event_log_path,
                    {
                        **event_base,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "type": "request_error",
                        "status": exc.code,
                        "elapsedSeconds": round(time.monotonic() - started, 3),
                        "error": body_text[:4000],
                    },
                )
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                error_text = str(redact(str(exc)))
                last_error = ModelRuntimeError(f"{label} direct JSON request failed: {error_text}")
                _append_model_event(
                    event_log_path,
                    {
                        **event_base,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "type": "request_error",
                        "elapsedSeconds": round(time.monotonic() - started, 3),
                        "error": error_text[:4000],
                    },
                )
        if attempt < model.request_max_retries:
            remaining = ensure_stage_time_remaining(label)
            delay = min(5.0, remaining) if remaining is not None else 5.0
            _append_model_event(
                event_log_path,
                {
                    **event_base,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "type": "retry_wait",
                    "delaySeconds": delay,
                },
            )
            time.sleep(delay)
    if envelope is None:
        assert last_error is not None
        raise last_error
    try:
        content = envelope["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ModelRuntimeError(f"{label} direct JSON response is invalid") from exc
    if not isinstance(result, dict):
        raise ModelRuntimeError(f"{label} direct JSON result must be an object")
    return result


def run_compiler(
    *,
    config: Stage2Config,
    contract: Stage1Contract,
    endpoint: str,
    seed: dict[str, Any],
    prompt: str,
    runtime_root: Path,
) -> dict[str, Any]:
    del contract, endpoint
    key = os.getenv(config.compiler.api_key_env, "").strip()
    base_url = os.getenv(config.compiler.base_url_env, "").strip()
    if not key or not base_url:
        raise ModelRuntimeError("compiler model environment is missing; configure OPENAI_BASE_URL and OPENAI_API_KEY")
    coverage_keys = [
        str(operation["coverageKey"])
        for operation in seed.get("operations", [])
        if isinstance(operation, dict) and operation.get("coverageKey")
    ]
    if not coverage_keys or len(coverage_keys) != len(seed.get("operations", [])):
        raise ModelRuntimeError("compiler seed contains missing coverage keys")
    runtime_root.mkdir(parents=True, exist_ok=True)
    result = _direct_json_request(
        model=config.compiler,
        base_url=base_url,
        api_key=key,
        system_prompt=COMPILER_SYSTEM,
        prompt=prompt,
        schema={
            "type": "object",
            "properties": {
                "acceptedCoverageKeys": {
                    "type": "array",
                    "items": {"type": "string", "enum": coverage_keys},
                    "minItems": len(coverage_keys),
                    "maxItems": len(coverage_keys),
                },
                "compilerRationale": {"type": "string", "minLength": 1},
            },
            "required": ["acceptedCoverageKeys", "compilerRationale"],
            "additionalProperties": False,
        },
        label="Stage 2 compiler",
        event_log_path=runtime_root / "model_events.jsonl",
    )
    atomic_write_json(runtime_root / "model_result.json", result)
    metadata = {
        "transport": "openai_json",
        "provider": config.compiler.provider,
        "model": config.compiler.name,
        "endpoint": endpoint_metadata(base_url),
        "requestTimeoutSeconds": config.compiler.request_timeout_seconds,
        "requestMaxRetries": config.compiler.request_max_retries,
        "totalStageTimeoutSeconds": config.total_stage_timeout_seconds,
    }
    atomic_write_json(runtime_root / "runtime_metadata.json", metadata)
    return {"plan": result, "_runtime": metadata}


def run_reviewer(
    *,
    config: Stage2Config,
    prompt: str,
    context_path: Path,
    runtime_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = os.getenv(config.reviewer.api_key_env, "").strip()
    base_url = os.getenv(config.reviewer.base_url_env, "").strip()
    if not key or not base_url:
        raise ModelRuntimeError("reviewer model environment is missing; configure OPENAI_BASE_URL and OPENAI_API_KEY")
    context = read_json_object(context_path)
    direct_prompt = prompt + "\n\nIndependent read-only review context:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    check_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "summary": {"type": "string", "minLength": 1},
            "evidenceRefs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "summary", "evidenceRefs"],
        "additionalProperties": False,
    }
    issue_schema = {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "category": {"type": "string", "minLength": 1},
            "path": {"type": "string", "minLength": 1},
            "message": {"type": "string", "minLength": 1},
            "evidenceRefs": {"type": "array", "items": {"type": "string"}},
            "requiredChange": {"type": "string", "minLength": 1},
            "acceptanceCriteria": {"type": "string", "minLength": 1},
            "affectedEndpoints": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "/openapi/paper/search",
                        "/openapi/scholar/search",
                        "/openapi/institutions/search",
                    ],
                },
            },
        },
        "required": [
            "severity",
            "category",
            "path",
            "message",
            "evidenceRefs",
            "requiredChange",
            "acceptanceCriteria",
            "affectedEndpoints",
        ],
        "additionalProperties": False,
    }
    check_names = (
        "sourceReplay",
        "informationCompleteness",
        "observationSemantics",
        "identity",
        "relationAuthority",
        "serialization",
        "competencyQueries",
    )
    schema = {
        "type": "object",
        "properties": {
            "schemaVersion": {"type": "string", "const": "dataelf-stage2-review.v2"},
            "verdict": {"type": "string", "enum": ["approve", "revise", "unusable"]},
            "summary": {"type": "string", "minLength": 1},
            "issues": {"type": "array", "items": issue_schema},
            "checkedEvidenceRefs": {"type": "array", "items": {"type": "string"}},
            "checks": {
                "type": "object",
                "properties": {name: check_schema for name in check_names},
                "required": list(check_names),
                "additionalProperties": False,
            },
        },
        "required": ["schemaVersion", "verdict", "summary", "issues", "checkedEvidenceRefs", "checks"],
        "additionalProperties": False,
    }
    review = _direct_json_request(
        model=config.reviewer,
        base_url=base_url,
        api_key=key,
        system_prompt=REVIEWER_SYSTEM,
        prompt=direct_prompt,
        schema=schema,
        label="Stage 2 reviewer",
        event_log_path=runtime_root / "model_events.jsonl",
    )
    atomic_write_json(runtime_root / "model_result.json", review)
    metadata = {
        "transport": "openai_json",
        "provider": config.reviewer.provider,
        "model": config.reviewer.name,
        "endpoint": endpoint_metadata(base_url),
        "requestTimeoutSeconds": config.reviewer.request_timeout_seconds,
        "requestMaxRetries": config.reviewer.request_max_retries,
        "totalStageTimeoutSeconds": config.total_stage_timeout_seconds,
        "freshContext": True,
    }
    atomic_write_json(runtime_root / "runtime_metadata.json", metadata)
    return review, metadata


__all__ = [
    "ModelRuntimeError",
    "endpoint_metadata",
    "ensure_stage_time_remaining",
    "run_compiler",
    "run_reviewer",
    "stage_deadline",
]
