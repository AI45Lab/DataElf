from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .provider import LLMProvider


_trace_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "dataelf_llm_trace_context",
    default={},
)


@contextmanager
def llm_trace_context(**updates: Any) -> Iterator[None]:
    current = dict(_trace_context.get())
    current.update({key: value for key, value in updates.items() if value is not None})
    token = _trace_context.set(current)
    try:
        yield
    finally:
        _trace_context.reset(token)


def current_llm_trace_context() -> dict[str, Any]:
    return dict(_trace_context.get())


@dataclass
class LLMTraceRecorder:
    env_id: str = "default"
    enabled: bool = True
    output_dir: Path = field(default_factory=lambda: Path(".elf") / "llm_traces")
    _buffers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_call(
        self,
        *,
        model: str,
        method: str,
        response_format: str,
        messages: list[dict[str, Any]],
        response_text: str | None,
        parsed_response: dict[str, Any] | None,
        status: str,
        latency_ms: int | None,
        request_options: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        if not self.enabled:
            return
        context = current_llm_trace_context()
        job_id = context.get("job_id")
        if not job_id:
            return

        created_at = int(time.time())
        scope = context.get("scope", "core")
        caller = context.get("caller", "unknown")
        response_text = response_text or ""
        meta = {
            "schema_version": "dataelf_llm_call_v1",
            "scope": scope,
            "caller": caller,
            "mode": context.get("mode", "unknown"),
            "attempt_id": context.get("attempt_id"),
            "method": method,
            "response_format": response_format,
            "status": status,
            "latency_ms": latency_ms,
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
            "parsed_response": parsed_response,
            "tool_name": context.get("tool_name"),
            "tool_component": context.get("tool_component"),
            "tool_call_context": context.get("tool_call_context"),
            "request_options": request_options or {},
            "usage": None,
            "prompt_hash": _hash_json(messages),
            "response_hash": _hash_text(response_text),
        }
        row = {
            "dataset_type": "dataelf_llm_call_tool" if scope == "tool" else "dataelf_llm_call_core",
            "dt": datetime.utcfromtimestamp(created_at).strftime("%Y-%m-%d"),
            "id": "",
            "session_id": job_id,
            "created_at": created_at,
            "step_id": 0,
            "env_id": self.env_id,
            "job_id": job_id,
            "is_terminal": False,
            "is_truncated": False,
            "step_reward": None,
            "reward": None,
            "messages": [_to_wind_tunnel_message(message) for message in messages],
            "response": _to_wind_tunnel_response(response_text, caller),
            "chosen_response": None,
            "rejected_response": None,
            "ground_truth_answer": None,
            "reference_answer": None,
            "agent_model": model,
            "env_name": "dataelf/tool" if scope == "tool" else "dataelf/core",
            "is_session_completed": False,
            "meta_json": json.dumps(meta, ensure_ascii=False),
            "blob_manifest": [],
        }
        with self._lock:
            buffer = self._buffers.setdefault(job_id, [])
            step_id = len(buffer) + 1
            row["step_id"] = step_id
            row["id"] = f"llmcall_{job_id}_{step_id:04d}_{uuid.uuid4().hex[:8]}"
            buffer.append(row)

    def finalize_job(self, job_id: str) -> Path | None:
        if not self.enabled:
            return None
        with self._lock:
            rows = self._buffers.pop(job_id, [])
        if not rows:
            return None
        for row in rows:
            row["is_session_completed"] = True
        rows[-1]["is_terminal"] = True

        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{job_id}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def discard_job(self, job_id: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._buffers.pop(job_id, None)


class TracingLLMProvider(LLMProvider):
    def __init__(self, inner: LLMProvider, recorder: LLMTraceRecorder):
        self.inner = inner
        self.recorder = recorder

    def generate(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        started_at = time.perf_counter()
        try:
            response = self._generate_from_messages(model, messages, **kwargs)
        except Exception as e:
            self._record_error(model, "generate", "text", messages, started_at, kwargs, e)
            raise
        self.recorder.record_call(
            model=model,
            method="generate",
            response_format="text",
            messages=messages,
            response_text=response,
            parsed_response=None,
            status="success",
            latency_ms=_elapsed_ms(started_at),
            request_options=_safe_request_options(kwargs),
        )
        return response

    def generate_json(self, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": "You must respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ]
        started_at = time.perf_counter()
        try:
            response = self._generate_json_text(model, messages, **kwargs)
            parsed = self._load_json_content(response)
        except Exception as e:
            self._record_error(model, "generate_json", "json", messages, started_at, kwargs, e)
            raise
        self.recorder.record_call(
            model=model,
            method="generate_json",
            response_format="json",
            messages=messages,
            response_text=response,
            parsed_response=parsed,
            status="success",
            latency_ms=_elapsed_ms(started_at),
            request_options=_safe_request_options(kwargs),
        )
        return parsed

    def _generate_from_messages(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        if hasattr(self.inner, "generate_from_messages"):
            return self.inner.generate_from_messages(model=model, messages=messages, **kwargs)
        prompt = str(messages[-1].get("content", ""))
        system_prompt = None
        if len(messages) > 1 and messages[0].get("role") == "system":
            system_prompt = str(messages[0].get("content", ""))
        return self.inner.generate(model=model, prompt=prompt, system_prompt=system_prompt, **kwargs)

    def _generate_json_text(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        if not hasattr(self.inner, "generate_from_messages"):
            parsed = self.inner.generate_json(model=model, prompt=str(messages[-1].get("content", "")), **kwargs)
            return json.dumps(parsed, ensure_ascii=False)

        try:
            return self.inner.generate_from_messages(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                **kwargs,
            )
        except Exception as e:
            if not _looks_like_response_format_incompatibility(e):
                raise
        return self.inner.generate_from_messages(model=model, messages=messages, **kwargs)

    def _load_json_content(self, content: str) -> dict[str, Any]:
        if hasattr(self.inner, "load_json_content"):
            return self.inner.load_json_content(content)
        return json.loads(content)

    def _record_error(
        self,
        model: str,
        method: str,
        response_format: str,
        messages: list[dict[str, Any]],
        started_at: float,
        kwargs: dict[str, Any],
        error: Exception,
    ) -> None:
        self.recorder.record_call(
            model=model,
            method=method,
            response_format=response_format,
            messages=messages,
            response_text="",
            parsed_response=None,
            status="error",
            latency_ms=_elapsed_ms(started_at),
            request_options=_safe_request_options(kwargs),
            error=error,
        )


def _to_wind_tunnel_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, list):
        content_items = content
    else:
        content_items = [{"type": "text", "text": "" if content is None else str(content)}]
    return {
        "role": message.get("role"),
        "content": content_items,
        "name": message.get("name"),
        "refusal": message.get("refusal"),
        "tool_calls": message.get("tool_calls") or [],
        "tool_call_id": message.get("tool_call_id"),
    }


def _to_wind_tunnel_response(response_text: str, caller: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": response_text}],
        "name": caller,
        "refusal": None,
        "tool_calls": [],
        "tool_call_id": None,
    }


def _safe_request_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key.lower() in {"api_key", "authorization", "headers"}:
            continue
        try:
            json.dumps(value)
        except TypeError:
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe


def _hash_json(value: Any) -> str:
    return _hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _elapsed_ms(started_at: float) -> int:
    return int(round((time.perf_counter() - started_at) * 1000))


def _looks_like_response_format_incompatibility(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in [
            "response_format",
            "json_object",
            "unsupported",
            "not support",
            "not supported",
            "invalid parameter",
            "unknown parameter",
            "unknown field",
            "unrecognized request argument",
            "extra inputs are not permitted",
        ]
    )
