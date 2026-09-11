from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from dataelf_server.analysis.insight_contract import (
    load_contract,
    load_source_evidence,
    validate_insights,
)
from dataelf_server.analysis.rdf_analysis import (
    RDFAnalysisError,
    load_analysis_manifest,
    load_candidate_signals,
)
from dataelf_server.analysis.writing import write_writing_review


class RDFFinalizeError(RuntimeError):
    pass


def materialize_rdf_insights(payload: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(str(payload.get("workspace") or "")).resolve()
    if not workspace.is_dir():
        raise RDFFinalizeError("workspace must be an existing directory")
    try:
        analysis_manifest = load_analysis_manifest(workspace)
        candidate_signals = load_candidate_signals(workspace)
    except RDFAnalysisError as exc:
        raise RDFFinalizeError(
            "verified Pi analysis is required before finalization: " + str(exc)
        ) from exc

    insights = payload.get("insights")
    if not isinstance(insights, list) or not insights:
        raise RDFFinalizeError("insights must contain at least one entry")

    (workspace / "insights").mkdir(parents=True, exist_ok=True)
    source_index = _load_scope_v2_source_index(workspace)
    contract = load_contract(workspace)
    signal_index = _candidate_signal_index(candidate_signals)
    manifest_artifacts = _verified_artifact_paths(
        workspace, analysis_manifest.get("artifacts", [])
    )
    normalized_insights: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(insights, start=1):
        if not isinstance(value, dict):
            raise RDFFinalizeError(f"insight {index} must be an object")
        insight_id = str(value.get("insight_id") or f"ins_{index:03d}").strip()
        if not re.fullmatch(r"ins_\d{3,}", insight_id) or insight_id in seen_ids:
            raise RDFFinalizeError(
                f"insight {index} must use a unique ins_<digits> ID"
            )
        seen_ids.add(insight_id)
        supporting_signal_ids = _text_list(
            value.get("supporting_signal_ids") or value.get("supporting_signals")
        )
        if not supporting_signal_ids:
            raise RDFFinalizeError(
                f"insight {index} must reference at least one verified candidate signal"
            )
        unknown_signals = [
            signal_id
            for signal_id in supporting_signal_ids
            if signal_id not in signal_index
        ]
        if unknown_signals:
            raise RDFFinalizeError(
                f"insight {index} references unknown candidate signals: "
                + ", ".join(unknown_signals)
            )
        supported_source_ids = {
            source_id
            for signal_id in supporting_signal_ids
            for source_id in _text_list(signal_index[signal_id].get("source_ids"))
        }
        supplied_source_ids = _text_list(
            value.get("source_ids") or value.get("sourceIds")
        )
        resolved_source_ids = [
            resolved
            for source_id in supplied_source_ids
            if (resolved := _resolve_source_id(source_id, source_index)) is not None
        ]
        if not resolved_source_ids:
            raise RDFFinalizeError(
                f"insight {index} must reference at least one valid Scope V2 source_id"
            )
        unsupported_sources = sorted(set(resolved_source_ids) - supported_source_ids)
        if unsupported_sources:
            # The model may select a real, verified source but omit the
            # candidate signal that established that source's analytical
            # provenance. Repair only that linkage by adding existing signals
            # which explicitly cite the source. No source, claim, or signal is
            # invented here; a source absent from every verified signal remains
            # a hard error.
            unresolved_sources: list[str] = []
            for source_id in unsupported_sources:
                proving_signals = [
                    signal_id
                    for signal_id, signal in signal_index.items()
                    if source_id in _text_list(signal.get("source_ids"))
                ]
                if not proving_signals:
                    unresolved_sources.append(source_id)
                    continue
                supporting_signal_ids.extend(
                    signal_id
                    for signal_id in proving_signals
                    if signal_id not in supporting_signal_ids
                )
            if unresolved_sources:
                raise RDFFinalizeError(
                    f"insight {index} cites sources not supported by any verified "
                    "candidate signal: " + ", ".join(unresolved_sources)
                )
        value = {**value, "source_ids": resolved_source_ids}
        support = _resolve_external_support(value, source_index)
        if not support:
            valid_ids = ", ".join(list(source_index)[:20])
            hint = f" Valid source_ids include: {valid_ids}." if valid_ids else ""
            raise RDFFinalizeError(
                f"insight {index} must reference at least one valid Scope V2 source_id."
                f"{hint}"
            )
        title = _required_text(value, "title", index)
        thesis = _required_text(value, "thesis", index)
        why_now = str(value.get("why_now") or "").strip() or (
            "该判断由本次数据窗口内的最新来源共同支持。"
        )
        counterarguments = _text_list(value.get("counterarguments"))
        compact_counterargument = str(value.get("counterargument") or "").strip()
        if compact_counterargument and not counterarguments:
            counterarguments = [compact_counterargument]
        if not counterarguments:
            counterarguments = ["当前结论受来源覆盖范围与数据时效性限制。"]
        next_questions = _text_list(value.get("next_questions")) or [
            "后续数据是否持续验证这一变化？"
        ]
        related_entities = list(
            dict.fromkeys(
                _text_list(value.get("related_entities"))
                + [
                    entity
                    for signal_id in supporting_signal_ids
                    for entity in _text_list(
                        signal_index[signal_id].get("related_entities")
                    )
                ]
            )
        )
        confidence = _confidence(value.get("confidence", 0.65), index)
        signal_artifacts = _verified_artifact_paths(
            workspace,
            [
                artifact
                for signal_id in supporting_signal_ids
                for artifact in _text_list(
                    signal_index[signal_id].get("analysis_artifacts")
                )
            ],
        )
        normalized_insights.append(
            {
                "insight_id": insight_id,
                "title": title,
                "thesis": thesis,
                "why_now": why_now,
                "supporting_signals": supporting_signal_ids,
                "analysis_artifacts": list(
                    dict.fromkeys(manifest_artifacts + signal_artifacts)
                ),
                "related_entities": related_entities,
                "external_support": support,
                "counterarguments": counterarguments,
                "confidence": confidence,
                "next_questions": next_questions,
            }
        )

    contract_issues: list[str] = []
    rejected_insights: list[dict[str, Any]] = []
    if contract is not None:
        contract_issues = validate_insights(
            normalized_insights,
            contract,
            source_evidence=load_source_evidence(workspace) or None,
        )

        # Contract violations are scoped to individual Insight items.  When a
        # batch contains both valid and invalid items, retain the grounded
        # subset instead of failing the entire job because one claim could not
        # be repaired by the model.  If every item is invalid, preserve the
        # complete proposal and issues so the bounded synthesis retry can still
        # attempt a correction.
        rejected_by_index = _contract_issues_by_index(contract_issues)
        if rejected_by_index and len(rejected_by_index) < len(normalized_insights):
            accepted_insights: list[dict[str, Any]] = []
            for index, insight in enumerate(normalized_insights, start=1):
                issues = rejected_by_index.get(index)
                if issues:
                    rejected_insights.append(
                        {
                            "insight_id": insight["insight_id"],
                            "issues": issues,
                        }
                    )
                else:
                    accepted_insights.append(insight)
            normalized_insights = accepted_insights
            contract_issues = validate_insights(
                normalized_insights,
                contract,
                source_evidence=load_source_evidence(workspace) or None,
            )

    final_brief = str(payload.get("finalBriefMarkdown") or "").strip() or (
        _final_brief(normalized_insights, language=(contract or {}).get("effective_output", {}).get("language", "zh-CN"))
    )

    _write_json(
        workspace / "insights" / "insight_candidates.json",
        {"insight_candidates": normalized_insights},
    )
    (workspace / "insights" / "final_brief.md").write_text(
        final_brief + "\n", encoding="utf-8"
    )
    result: dict[str, Any] = {
        "signal_count": len(candidate_signals),
        "insight_count": len(normalized_insights),
    }
    if rejected_insights:
        result["rejected_insight_count"] = len(rejected_insights)
        result["rejected_insights"] = rejected_insights
    if contract_issues:
        # Keep the proposal as a provisional artifact so the bounded synthesis
        # retry can rewrite it with exact, actionable validation feedback.
        result["contract_issues"] = contract_issues
    writing = write_writing_review(workspace, normalized_insights, contract or {})
    if writing:
        result["writing_warnings"] = writing["warnings"]
    return result


def _contract_issues_by_index(issues: list[str]) -> dict[int, list[str]]:
    indexed: dict[int, list[str]] = {}
    for issue in issues:
        match = re.match(r"^Insight (\d+)\b", issue)
        if match is None:
            continue
        indexed.setdefault(int(match.group(1)), []).append(issue)
    return indexed


def _load_scope_v2_source_index(workspace: Path) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for path in sorted(workspace.glob("scope_v2/*/result.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = document.get("sources") if isinstance(document, dict) else None
        if not isinstance(sources, dict):
            continue
        for source_block in sources.values():
            items = source_block.get("items") if isinstance(source_block, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("source_id") or "").strip()
                title = str(item.get("title") or "").strip()
                url = str(item.get("url") or "").strip()
                if source_id and title and url:
                    index[source_id] = {
                        "source": str(item.get("source") or "").strip(),
                        "source_id": source_id,
                        "title": title,
                        "url": url,
                    }
    return index


def _candidate_signal_index(
    signals: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for position, signal in enumerate(signals, start=1):
        signal_id = str(signal.get("signal_id") or "").strip()
        if not signal_id or signal_id in index:
            raise RDFFinalizeError(
                f"candidate signal {position} must have a unique signal_id"
            )
        index[signal_id] = signal
    return index


def _verified_artifact_paths(workspace: Path, values: Any) -> list[str]:
    paths: list[str] = []
    for value in values if isinstance(values, list) else []:
        relative = str(value or "").strip()
        if not relative:
            continue
        path = (workspace / relative).resolve()
        if (
            not path.is_relative_to(workspace)
            or not path.is_file()
            or path.stat().st_size <= 0
        ):
            raise RDFFinalizeError(
                f"verified analysis artifact is missing or empty: {relative}"
            )
        paths.append(str(path.relative_to(workspace)))
    return list(dict.fromkeys(paths))


def _resolve_external_support(
    value: dict[str, Any], source_index: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    records = _normalize_external_support(value.get("external_support"))
    source_ids = _text_list(value.get("source_ids") or value.get("sourceIds"))
    for source_id in source_ids:
        resolved_id = _resolve_source_id(source_id, source_index)
        if resolved_id is not None:
            records.append(source_index[resolved_id])
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        source_id = record.get("source_id", "")
        merged = {**source_index.get(source_id, {}), **record}
        if not merged.get("title") or not merged.get("url"):
            continue
        identity = (merged.get("source_id", ""), merged["url"])
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(merged)
    return normalized


def _resolve_source_id(
    supplied_id: str, source_index: dict[str, dict[str, str]]
) -> str | None:
    """Resolve exact IDs plus unambiguous model abbreviations.

    Models sometimes preserve the source prefix but shorten a long opaque ID.
    Accepting only a unique prefix remains grounded in the prefetched data. When
    the task has exactly one source, any non-empty reference is also
    unambiguous and can safely resolve to that source.
    """

    candidate = supplied_id.strip()
    if not candidate:
        return None
    if candidate in source_index:
        return candidate
    if len(source_index) == 1:
        return next(iter(source_index))

    lowered = candidate.casefold()
    suffix = lowered.split(":", 1)[-1]
    if len(suffix) < 8:
        return None
    matches = [
        source_id
        for source_id in source_index
        if source_id.casefold().startswith(lowered)
        or source_id.casefold().split(":", 1)[-1].startswith(suffix)
    ]
    return matches[0] if len(matches) == 1 else None


def _final_brief(insights: list[dict[str, Any]], *, language: str = "zh-CN") -> str:
    heading = "Final brief" if language == "en" else "最终简报"
    sections = [f"# {heading}"]
    for insight in insights:
        sections.extend([f"## {insight['title']}", insight["thesis"]])
    return "\n\n".join(sections)


def _normalize_external_support(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in value:
        if not isinstance(record, dict):
            continue
        item = {
            key: str(record.get(key) or "").strip()
            for key in ("source", "source_id", "title", "url")
            if str(record.get(key) or "").strip()
        }
        identity = (item.get("source_id", ""), item.get("url", ""))
        if not item or identity in seen:
            continue
        seen.add(identity)
        normalized.append(item)
    return normalized


def _required_text(value: dict[str, Any], key: str, index: int) -> str:
    text = str(value.get(key) or "").strip()
    if not text:
        raise RDFFinalizeError(f"insight {index} {key} is required")
    return text


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _confidence(value: Any, index: int) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise RDFFinalizeError(f"insight {index} confidence is invalid") from exc
    if not 0 <= confidence <= 1:
        raise RDFFinalizeError(f"insight {index} confidence must be between 0 and 1")
    return confidence


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m dataelf_server.analysis.rdf_finalize PAYLOAD.json")
    payload_path = Path(sys.argv[1]).resolve()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    result = materialize_rdf_insights(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
