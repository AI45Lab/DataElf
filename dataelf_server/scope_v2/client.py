from __future__ import annotations

from dataelf.discovery.run_control import check_cancelled

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ScopeV2AIIndexError(Exception):
    code: str
    message: str
    trace_id: str | None = None
    http_status: int | None = None

    def __str__(self) -> str:
        suffix = f" trace_id={self.trace_id}" if self.trace_id else ""
        return f"{self.code}: {self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "trace_id": self.trace_id,
            "http_status": self.http_status,
        }


class ScopeV2AIIndexClient:
    """Small authenticated client for the five endpoints used by scope_v2."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30,
        min_request_interval_seconds: float = 0.1,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self._opener = opener
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-AI-Index-Key": self.api_key,
                "User-Agent": "dataelf-api-scope-v2/0.1",
            },
            method="POST",
        )

        last_error: ScopeV2AIIndexError | None = None
        check_cancelled()
        for attempt in range(3):
            check_cancelled()
            self._respect_rate_limit()
            check_cancelled()
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    return self._validate(self._decode(response.read()))
            except urllib.error.HTTPError as exc:
                detail = self._decode_error(exc.read())
                error = ScopeV2AIIndexError(
                    code=str(detail.get("code") or f"http_{exc.code}"),
                    message=str(
                        detail.get("message")
                        or detail.get("msg")
                        or "AI Index HTTP error"
                    ),
                    trace_id=_optional_string(detail.get("trace_id")),
                    http_status=exc.code,
                )
                last_error = error
                if (exc.code == 429 or 500 <= exc.code < 600) and attempt < 2:
                    self._sleeper(_retry_delay(exc, attempt))
                    continue
                raise error from exc
            except urllib.error.URLError as exc:
                error = ScopeV2AIIndexError(
                    code="network_error",
                    message=str(exc.reason),
                )
                last_error = error
                if attempt < 2:
                    self._sleeper(2**attempt)
                    continue
                raise error from exc

        assert last_error is not None
        raise last_error

    def _respect_rate_limit(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            if elapsed < self.min_request_interval_seconds:
                self._sleeper(self.min_request_interval_seconds - elapsed)
        self._last_request_at = self._monotonic()

    @staticmethod
    def _decode(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ScopeV2AIIndexError(
                code="invalid_response",
                message="AI Index returned non-JSON content",
            ) from exc

    @classmethod
    def _decode_error(cls, raw: bytes) -> dict[str, Any]:
        try:
            value = cls._decode(raw)
        except ScopeV2AIIndexError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _validate(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ScopeV2AIIndexError(
                code="invalid_response", message="response root must be an object"
            )
        if result.get("code") not in (0, "0"):
            raise ScopeV2AIIndexError(
                code=str(result.get("code", "business_error")),
                message=str(
                    result.get("msg")
                    or result.get("message")
                    or "AI Index business error"
                ),
                trace_id=_optional_string(result.get("trace_id")),
            )
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ScopeV2AIIndexError(
                code="invalid_response", message="data.list must be an array"
            )
        return result


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    headers = getattr(exc, "headers", None)
    if headers is not None:
        value = headers.get("Retry-After")
        if value:
            try:
                return max(float(value), 0.0)
            except ValueError:
                pass
    return float(2**attempt)


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
