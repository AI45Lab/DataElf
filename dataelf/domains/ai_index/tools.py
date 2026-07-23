from __future__ import annotations

from typing import Any

from dataelf.domains.ai_index.client import AIIndexClient


class AIIndexDiscoveryTools:
    def __init__(self, client: AIIndexClient):
        self.client = client

    def search_papers(self, **kwargs: Any) -> dict[str, Any]:
        return _compact(self.client.search_papers(**kwargs))

    def search_institutions(self, **kwargs: Any) -> dict[str, Any]:
        return _compact(self.client.search_institutions(**kwargs))

    def search_scholars(self, **kwargs: Any) -> dict[str, Any]:
        return _compact(self.client.search_scholars(**kwargs))

    def fetch_institution_funding(self, institution_id: str) -> dict[str, Any]:
        return _compact(self.client.fetch_institution_funding(institution_id))


def _compact(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data", {})
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        previews = data["list"][:10]
        total = data.get("total", len(previews))
    else:
        previews = data
        total = None
    return {
        "source": response.get("source"),
        "mode": response.get("mode"),
        "endpoint": response.get("endpoint"),
        "request": response.get("request", {}),
        "trace_id": response.get("trace_id"),
        "raw_uri": response.get("raw_uri"),
        "total": total,
        "preview": previews,
    }
