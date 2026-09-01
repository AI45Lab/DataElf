from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index.modeling.contracts import RawAcquisitionRecord, RawAcquisitionResult
from dataelf.domains.ai_index.connector import AIIndexConnector
from dataelf.discovery.contracts import DiscoveryJob


class AIIndexRawCollector:
    """Collect the AI Index raw corpus required by the modeling contract."""

    def __init__(
        self,
        *,
        mode: str,
        base_url: str,
        api_key: str,
        fixtures_dir: Path,
        page_size: int = 50,
    ):
        self.mode = mode
        self.base_url = base_url
        self.api_key = api_key
        self.fixtures_dir = fixtures_dir
        self.page_size = max(1, min(int(page_size), 50))

    def collect(self, job: DiscoveryJob, workspace: Path) -> RawAcquisitionResult:
        connector = AIIndexConnector(
            mode=self.mode,
            base_url=self.base_url,
            api_key=self.api_key,
            fixtures_dir=self.fixtures_dir,
            # The modeling stage owns its three raw envelopes. Avoid the connector's
            # persistence hook so API schema adaptation cannot create a second
            # set of files or touch the normal workspace CSV path.
            workspace_path=None,
        )
        query = self._query(job, self.mode)
        responses = [
            connector.search_papers(**query, sort_type="heat"),
            # The production institutions endpoint does not accept `heat`;
            # its equivalent default ranking is named `index`.
            connector.search_institutions(**query, sort_type="index"),
            connector.search_scholars(**query, sort_type="heat"),
        ]
        records: list[RawAcquisitionRecord] = []
        for response in responses:
            canonical = _canonical_envelope(response)
            raw_file = str(_persist_raw_envelope(workspace, canonical).resolve())
            data = canonical.get("data")
            items = data.get("list", []) if isinstance(data, dict) else []
            records.append(
                RawAcquisitionRecord(
                    endpoint=str(canonical.get("endpoint", "")),
                    request=dict(canonical.get("request", {})) if isinstance(canonical.get("request"), dict) else {},
                    raw_file=raw_file,
                    item_count=len(items) if isinstance(items, list) else 0,
                )
            )
        raw_files = sorted(str(path.resolve()) for path in (workspace / "raw" / "ai_index").glob("*.json") if path.is_file())
        return RawAcquisitionResult(raw_files=raw_files, records=records)

    def _query(self, job: DiscoveryJob, mode: str) -> dict[str, Any]:
        query: dict[str, Any] = {"page": 1, "size": self.page_size}
        if mode == "fixture":
            return query
        domains = job.spec.parameters.get("domains")
        sub_domains = job.spec.parameters.get("sub_domains")
        if isinstance(domains, list) and domains:
            query["domains"] = [str(item) for item in domains]
        if isinstance(sub_domains, list) and sub_domains:
            query["sub_domains"] = [str(item) for item in sub_domains]
        return query


def _canonical_envelope(response: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(response.get("endpoint", ""))
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    rows = data.get("list", []) if isinstance(data.get("list"), list) else []
    if "paper/search" in endpoint:
        canonical_rows = [_canonical_paper(item) for item in rows if isinstance(item, dict)]
    elif "scholar/search" in endpoint:
        canonical_rows = [_canonical_scholar(item) for item in rows if isinstance(item, dict)]
    elif "institutions/search" in endpoint:
        canonical_rows = [_canonical_institution(item) for item in rows if isinstance(item, dict)]
    else:
        canonical_rows = [dict(item) for item in rows if isinstance(item, dict)]
    raw = response.get("raw")
    if not isinstance(raw, dict):
        raw = {"data": data}
    return {
        "source": response.get("source", "ai_index"),
        "mode": response.get("mode"),
        "method": response.get("method"),
        "endpoint": endpoint,
        "request": response.get("request", {}),
        "trace_id": response.get("trace_id"),
        "data": {"total": data.get("total", len(canonical_rows)), "list": canonical_rows},
        # Exact API/fixture response remains replayable; data is the formal
        # Ontology Stage 1 semantic projection of the same response.
        "raw": raw,
    }


def _canonical_paper(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    _copy_first(result, "id", item, "id", "paper_id")
    _copy_first(result, "title", item, "title")
    _copy_first(result, "abstract", item, "abstract")
    _copy_first(result, "published_at", item, "published_at", "pub_date")
    _copy_first(result, "venue", item, "venue", "conference_abbreviation", "conference_name", "journal_abbreviation", "journal_name")
    _copy_first(result, "fields", item, "fields", "domains")
    _copy_first(result, "citation_count", item, "citation_count", "cited_by_count")
    authors = item.get("author_ids")
    if not isinstance(authors, list) and isinstance(item.get("authors"), list):
        authors = [entry.get("author_id") for entry in item["authors"] if isinstance(entry, dict) and entry.get("author_id") not in (None, "")]
    if isinstance(authors, list):
        result["author_ids"] = authors
    institutions = item.get("institution_ids")
    if not isinstance(institutions, list):
        singular = item.get("institution_id")
        institutions = [singular] if singular not in (None, "") else None
    if isinstance(institutions, list):
        result["institution_ids"] = institutions
    awards = item.get("awards")
    if not isinstance(awards, list):
        info = item.get("conf_award_info")
        awards = info.get("awards") if isinstance(info, dict) else None
    if isinstance(awards, list):
        result["awards"] = _canonical_awards(awards)
    hotness = item.get("hotness")
    if not isinstance(hotness, dict) and item.get("heat") not in (None, ""):
        hotness = {"half_year": item["heat"]}
        if item.get("previous_heat") not in (None, ""):
            hotness["previous_half_year"] = item["previous_heat"]
    if isinstance(hotness, dict):
        result["hotness"] = hotness
    return result


def _canonical_scholar(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    _copy_first(result, "id", item, "id", "scholar_id")
    _copy_first(result, "name", item, "name", "display_name")
    _copy_first(result, "email", item, "email")
    _copy_first(result, "homepage", item, "homepage")
    _copy_first(result, "fields", item, "fields", "domains")
    for key in ("paper_ids", "institution_ids", "awards", "venues"):
        value = item.get(key)
        if isinstance(value, list):
            result[key] = value
    if "venues" not in result:
        venues = []
        for key in ("conference_names", "journal_names"):
            if isinstance(item.get(key), list):
                venues.extend(item[key])
        if venues:
            result["venues"] = list(dict.fromkeys(str(value) for value in venues if value not in (None, "")))
    if "awards" not in result and isinstance(item.get("award_list"), list):
        result["awards"] = _canonical_awards(item["award_list"])
    elif "awards" in result:
        result["awards"] = _canonical_awards(result["awards"])
    if "institution_ids" not in result and item.get("institution_id") not in (None, ""):
        result["institution_ids"] = [item["institution_id"]]
    hotness = item.get("hotness")
    if not isinstance(hotness, dict) and item.get("heat") not in (None, ""):
        hotness = {"half_year": item["heat"]}
        if item.get("previous_heat") not in (None, ""):
            hotness["previous_half_year"] = item["previous_heat"]
    if isinstance(hotness, dict):
        result["hotness"] = hotness
    return result


def _canonical_institution(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for target, sources in {
        "id": ("id", "institution_id"),
        "name": ("name",),
        "country": ("country", "country_code"),
        "region": ("region",),
        "fields": ("fields", "domains"),
        "paper_count": ("paper_count",),
        "scholar_count": ("scholar_count", "author_count"),
        "funding_total_usd": ("funding_total_usd",),
    }.items():
        _copy_first(result, target, item, *sources)
    for key in ("related_paper_ids", "related_scholar_ids", "news"):
        if isinstance(item.get(key), list):
            result[key] = item[key]
    hotness = item.get("hotness")
    if not isinstance(hotness, dict) and item.get("heat") not in (None, ""):
        hotness = {"half_year": item["heat"]}
        if item.get("previous_heat") not in (None, ""):
            hotness["previous_half_year"] = item["previous_heat"]
    if isinstance(hotness, dict):
        result["hotness"] = hotness
    impact = item.get("impact")
    if isinstance(impact, dict):
        result["impact"] = impact
    return result


def _copy_first(target: dict[str, Any], target_key: str, source: dict[str, Any], *source_keys: str) -> None:
    for key in source_keys:
        if key in source and source[key] not in (None, ""):
            target[target_key] = source[key]
            return


def _canonical_awards(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value)
        elif isinstance(value, dict):
            label = value.get("title") or value.get("name") or value.get("key")
            if label not in (None, ""):
                result.append(str(label))
    return list(dict.fromkeys(result))


def _persist_raw_envelope(workspace: Path, payload: dict[str, Any]) -> Path:
    raw_dir = workspace / "raw" / "ai_index"
    raw_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    endpoint_slug = str(payload["endpoint"]).strip("/").replace("/", "_")
    path = raw_dir / f"{endpoint_slug}_{digest[:12]}.json"
    temporary = path.with_name(f".{path.name}.{digest[:8]}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


__all__ = ["AIIndexRawCollector"]
