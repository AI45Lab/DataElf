from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def public_insight(insight: dict[str, Any], workspace_path: Path) -> dict[str, Any]:
    """Convert an internal DataElf insight into the compact public contract."""

    source_index = _load_source_index(workspace_path)
    return _public_insight(insight, source_index)


def public_insights(
    insights: list[dict[str, Any]], workspace_path: Path
) -> list[dict[str, Any]]:
    """Convert all insights while indexing workspace sources only once."""

    source_index = _load_source_index(workspace_path)
    return [_public_insight(insight, source_index) for insight in insights]


def _public_insight(
    insight: dict[str, Any], source_index: dict[str, dict[str, str]]
) -> dict[str, Any]:
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    support = insight.get("external_support")
    if isinstance(support, list):
        for reference in support:
            if not isinstance(reference, dict):
                continue
            indexed = _lookup_source(reference, source_index)
            title = _source_text(reference.get("title")) or indexed.get("title", "")
            url = _source_url(reference) or indexed.get("url", "")
            if not url:
                url = _url_from_source_id(reference.get("source_id"))
            normalized_url = _normalize_identifier(url)
            if not title or not normalized_url or normalized_url in seen_urls:
                continue
            sources.append({"title": title, "url": url})
            seen_urls.add(normalized_url)

    return {
        "insight_id": _clean_text(insight.get("insight_id")),
        "title": _clean_text(insight.get("title")),
        "content": _clean_text(insight.get("thesis")),
        "sources": sources,
    }


def _load_source_index(workspace_path: Path) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for path in sorted(workspace_path.glob("scope_v2/*/result.json")):
        document = _read_json(path)
        sources = document.get("sources") if isinstance(document, dict) else None
        if not isinstance(sources, dict):
            continue
        for source in sources.values():
            items = source.get("items") if isinstance(source, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                title = _first_text(
                    item,
                    data,
                    keys=("title", "name", "news_title", "paper_title"),
                )
                url = _first_text(item, data, keys=("url", "link", "html_url"))
                _add_to_index(index, title, url, item, data)

    # Legacy scope and ontology artifacts use raw AI Index envelopes instead of
    # the Scope V2 aggregate. Index their records as a compatibility fallback.
    for path in sorted((workspace_path / "raw" / "ai_index").glob("*.json")):
        _index_nested_records(_read_json(path), index)
    return index


def _index_nested_records(value: Any, index: dict[str, dict[str, str]]) -> None:
    if isinstance(value, dict):
        title = _first_text(
            value,
            keys=("title", "name", "news_title", "paper_title"),
        )
        url = _first_text(value, keys=("url", "link", "html_url"))
        if title or url:
            _add_to_index(index, title, url, value)
        for child in value.values():
            if isinstance(child, (dict, list)):
                _index_nested_records(child, index)
    elif isinstance(value, list):
        for child in value:
            _index_nested_records(child, index)


def _add_to_index(
    index: dict[str, dict[str, str]],
    title: str,
    url: str,
    *records: dict[str, Any],
) -> None:
    if not title and not url:
        return
    identifiers: list[str] = []
    keys = (
        "source_id",
        "id",
        "news_id",
        "paper_id",
        "scholar_id",
        "institution_id",
        "url",
        "link",
        "html_url",
    )
    for record in records:
        for key in keys:
            identifier = _normalize_identifier(record.get(key))
            if identifier:
                identifiers.append(identifier)
                if ":http" in identifier:
                    identifiers.append(identifier[identifier.index("http") :])
    if url:
        identifiers.append(_normalize_identifier(url))
    for identifier in identifiers:
        existing = index.setdefault(identifier, {})
        if title:
            existing.setdefault("title", title)
        if url:
            existing.setdefault("url", url)


def _lookup_source(
    reference: dict[str, Any], index: dict[str, dict[str, str]]
) -> dict[str, str]:
    found: dict[str, str] = {}
    for key in ("source_id", "url", "id"):
        identifier = _normalize_identifier(reference.get(key))
        if not identifier:
            continue
        candidates = [identifier]
        if ":http" in identifier:
            candidates.append(identifier[identifier.index("http") :])
        for candidate in candidates:
            indexed = index.get(candidate, {})
            if indexed.get("title"):
                found.setdefault("title", indexed["title"])
            if indexed.get("url"):
                found.setdefault("url", indexed["url"])
    return found


def _source_url(record: dict[str, Any]) -> str:
    return _first_text(record, keys=("url", "link", "html_url"))


def _url_from_source_id(value: Any) -> str:
    source_id = _clean_text(value)
    position = source_id.find("http")
    return source_id[position:] if position >= 0 else ""


def _first_text(*records: dict[str, Any], keys: tuple[str, ...]) -> str:
    for record in records:
        for key in keys:
            text = _source_text(record.get(key))
            if text:
                return text
    return ""


def _normalize_identifier(value: Any) -> str:
    return _clean_text(value).rstrip("/").casefold()


def _clean_text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _source_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
