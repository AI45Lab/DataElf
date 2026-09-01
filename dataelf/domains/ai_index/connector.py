from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from dataelf.domains.ai_index.config import DEFAULT_AI_INDEX_API_KEY, DEFAULT_AI_INDEX_BASE_URL


AI_INDEX_ENDPOINTS = {
    "search_papers": "/openapi/paper/search",
    "search_institutions": "/openapi/institutions/search",
    "search_scholars": "/openapi/scholar/search",
    "fetch_institution_funding": "/openapi/institutions/{institution_id}/funding-profile",
}


class AIIndexConnector:
    name = "ai_index"

    def __init__(
        self,
        mode: str = "fixture",
        base_url: str = DEFAULT_AI_INDEX_BASE_URL,
        api_key: str = DEFAULT_AI_INDEX_API_KEY,
        fixtures_dir: Path | None = None,
        workspace_path: Path | None = None,
        timeout_seconds: int = 30,
    ):
        self.mode = mode
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.fixtures_dir = fixtures_dir or Path("fixtures/ai_index")
        self.workspace_path = workspace_path
        self.timeout_seconds = timeout_seconds
        self.min_request_interval_seconds = 0.1
        self._last_request_at = 0.0
        self._fixtures: dict[str, list[dict[str, Any]]] = {}
        if self.mode == "fixture":
            self._fixtures = {
                "papers": self._load_fixture("papers.json"),
                "institutions": self._load_fixture("institutions.json"),
                "scholars": self._load_fixture("scholars.json"),
            }

    def search_papers(self, **kwargs: Any) -> dict[str, Any]:
        return self._post_search("papers", "search_papers", kwargs)

    def search_institutions(self, **kwargs: Any) -> dict[str, Any]:
        return self._post_search("institutions", "search_institutions", kwargs)

    def search_scholars(self, **kwargs: Any) -> dict[str, Any]:
        return self._post_search("scholars", "search_scholars", kwargs)

    def fetch_institution_funding(self, institution_id: str) -> dict[str, Any]:
        endpoint = AI_INDEX_ENDPOINTS["fetch_institution_funding"].format(institution_id=institution_id)
        if self.mode == "fixture":
            raw = self._fixture_funding(institution_id)
            return self._wrap("GET", endpoint, {"institution_id": institution_id}, raw)
        return self._request("GET", endpoint, {"institution_id": institution_id})

    def _post_search(self, fixture_name: str, endpoint_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = AI_INDEX_ENDPOINTS[endpoint_key]
        clean_payload = {key: value for key, value in payload.items() if value not in (None, [], {})}
        if self.mode == "fixture":
            rows = self._filter_fixture(self._fixtures[fixture_name], clean_payload)
            page = int(clean_payload.get("page", 1))
            size = min(int(clean_payload.get("size", 10)), 50)
            start = (page - 1) * size
            raw = {"code": 0, "msg": "success", "trace_id": "fixture", "data": {"total": len(rows), "list": rows[start : start + size]}}
            return self._wrap("POST", endpoint, clean_payload, raw)
        return self._request("POST", endpoint, clean_payload)

    def _request(self, method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if method == "POST" else None
        url = f"{self.base_url}{endpoint}"
        headers = {"X-AI-Index-Key": self.api_key}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(3):
            self._respect_rate_limit()
            req = request.Request(url, data=body, headers=headers, method=method)
            try:
                with request.urlopen(req, timeout=self.timeout_seconds) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                    return self._wrap(method, endpoint, payload, raw)
            except error.HTTPError as exc:
                last_error = exc
                if exc.code == 429 and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                details = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"AI Index API error {exc.code} for {endpoint}: {details}") from exc
            except error.URLError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"AI Index API network error for {endpoint}: {exc}") from exc
        raise RuntimeError(f"AI Index API request failed for {endpoint}: {last_error}")

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_request_interval_seconds:
            time.sleep(self.min_request_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _wrap(self, method: str, endpoint: str, payload: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict) and raw.get("code", 0) not in (0, "0"):
            raise RuntimeError(f"AI Index business error for {endpoint}: {raw}")
        wrapped = {
            "source": self.name,
            "mode": self.mode,
            "method": method,
            "endpoint": endpoint,
            "request": payload,
            "trace_id": raw.get("trace_id") if isinstance(raw, dict) else None,
            "data": raw.get("data", {}) if isinstance(raw, dict) else {},
            "raw": raw,
        }
        raw_uri = self._persist_workspace_raw(wrapped)
        if raw_uri:
            wrapped["raw_uri"] = raw_uri
        return wrapped

    def _persist_workspace_raw(self, payload: dict[str, Any]) -> str | None:
        if not self.workspace_path:
            return None
        raw_dir = self.workspace_path / "raw" / "ai_index"
        raw_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        endpoint_slug = payload["endpoint"].strip("/").replace("/", "_").replace("{", "").replace("}", "")
        path = raw_dir / f"{endpoint_slug}_{digest[:12]}.json"
        if not path.exists():
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return str(path)

    def _load_fixture(self, filename: str) -> list[dict[str, Any]]:
        path = self.fixtures_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"AI Index fixture not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _filter_fixture(self, rows: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
        topic_terms = _as_terms(payload.get("sub_domains")) + _as_terms(payload.get("domains"))
        keyword = str(payload.get("keyword") or payload.get("title") or payload.get("name") or "").casefold()
        filtered = rows
        if topic_terms:
            filtered = [row for row in filtered if _matches_terms(row, topic_terms)]
        if keyword:
            filtered = [row for row in filtered if keyword in json.dumps(row, ensure_ascii=False).casefold()]
        sort_type = payload.get("sort_type", "heat")
        if sort_type in {"heat", "index"}:
            filtered = sorted(filtered, key=_fixture_heat, reverse=True)
        elif sort_type in {"citation_count", "paperCount"}:
            filtered = sorted(filtered, key=lambda row: int(row.get("citation_count") or row.get("paper_count") or 0), reverse=True)
        elif sort_type == "publish_time":
            filtered = sorted(filtered, key=lambda row: str(row.get("pub_date") or row.get("published_at") or ""), reverse=True)
        return filtered

    def _fixture_funding(self, institution_id: str) -> dict[str, Any]:
        institutions = self._fixtures.get("institutions", [])
        match = next((row for row in institutions if row.get("id") == institution_id or row.get("institution_id") == institution_id), None)
        if not match:
            match = {"id": institution_id, "name": institution_id, "funding_total_usd": 0}
        value = int(match.get("funding_total_usd") or 0)
        data = {
            "summary": {
                "total_funding": {"currency": "USD", "value": value, "value_usd": value},
                "funding_round_count": 1 if value else 0,
                "investor_count": 0,
                "lead_investor_count": 0,
                "invested_round_count": 0,
                "lead_invested_round_count": 0,
            },
            "funding": {
                "financials_highlights": {
                    "funding_total": {"currency": "USD", "value": value, "value_usd": value},
                    "num_funding_rounds": 1 if value else 0,
                    "num_investors": 0,
                    "num_lead_investors": 0,
                },
                "funding_rounds": [],
                "investors": [],
            },
            "invested": {"investment_count": 0, "lead_investment_count": 0, "investments": []},
        }
        return {"code": 0, "msg": "success", "trace_id": "fixture", "data": data}


def _as_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.casefold()]
    return [str(item).casefold() for item in value]


def _matches_terms(row: dict[str, Any], terms: list[str]) -> bool:
    haystack = " ".join(
        [
            " ".join(str(item) for item in row.get("fields", [])),
            " ".join(str(item) for item in row.get("domains", [])),
            " ".join(str(item) for item in row.get("sub_domains", [])),
            " ".join(str(item) for item in row.get("sub_tags", [])),
            str(row.get("abstract", "")),
            str(row.get("title", "")),
            str(row.get("name", "")),
        ]
    ).casefold()
    return any(term in haystack for term in terms)


def _fixture_heat(row: dict[str, Any]) -> int:
    heat = row.get("heat")
    if isinstance(heat, int):
        return heat
    if isinstance(heat, dict):
        return int(heat.get("half_year") or heat.get("month") or 0)
    radar = row.get("index_radar_display") or {}
    return int(radar.get("total_score") or 0)
