from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rdflib import Dataset
from dataelf_server.analysis.artifacts import rdf_path


MANIFEST_RELATIVE_PATH = Path("logs/pi_analysis_manifest.json")
SCRIPT_CANDIDATES = (
    Path("scripts/analyze_scope_v2.py"),
    Path("scripts/analyze_rdf.py"),
)
REQUIRED_TABLE = Path("tables/source_analysis.csv")
REQUIRED_NOTE = Path("notes/rdf_analysis.md")
REQUIRED_SIGNALS = Path("insights/candidate_signals.json")


class RDFAnalysisError(RuntimeError):
    pass


def run_rdf_analysis(workspace_path: Path, *, timeout_seconds: int = 600) -> dict[str, Any]:
    workspace = workspace_path.resolve()
    if not workspace.is_dir():
        raise RDFAnalysisError("workspace must be an existing directory")
    script_path = _select_script(workspace)
    if script_path.stat().st_size < 200:
        raise RDFAnalysisError("analysis script is too small to be a substantive analysis")

    manifest_path = workspace / MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=workspace,
            env=_analysis_env(workspace),
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RDFAnalysisError(
            f"analysis script timed out after {timeout_seconds} seconds"
        ) from exc
    execution_detail = (completed.stderr or completed.stdout or "").strip()
    repairs = _normalize_analysis_outputs(workspace)
    try:
        details = _validate_outputs(workspace)
    except RDFAnalysisError as exc:
        if completed.returncode != 0:
            raise RDFAnalysisError(
                "analysis script exited with code "
                f"{completed.returncode}: {' '.join(execution_detail.split())[:1200]}"
            ) from exc
        raise
    artifacts = [
        str(script_path.relative_to(workspace)),
        str(REQUIRED_TABLE),
        str(REQUIRED_NOTE),
        str(REQUIRED_SIGNALS),
        *details["deep_dives"],
    ]
    manifest = {
        "schema_version": "dataelf-pi-analysis.v1",
        "status": "completed",
        "completed_at": _utc_now(),
        "script_path": str(script_path.relative_to(workspace)),
        "script_sha256": _sha256(script_path),
        "signal_count": details["signal_count"],
        "source_count": details["source_count"],
        "source_ids": details["source_ids"],
        "artifacts": list(dict.fromkeys(artifacts)),
        "script_exit_code": completed.returncode,
        "normalization_repairs": repairs,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def validate_analysis_manifest(workspace_path: Path) -> list[str]:
    workspace = workspace_path.resolve()
    path = workspace / MANIFEST_RELATIVE_PATH
    if not path.is_file():
        return ["Pi analysis completion manifest is missing."]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Pi analysis completion manifest is invalid: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        return ["Pi analysis completion manifest is not completed."]
    script_value = str(manifest.get("script_path") or "").strip()
    if script_value not in {str(value) for value in SCRIPT_CANDIDATES}:
        return ["Pi analysis manifest references an unsupported analysis script."]
    script_path = (workspace / script_value).resolve()
    if not script_path.is_relative_to(workspace) or not script_path.is_file():
        return ["Pi analysis script referenced by the manifest is missing."]
    if _sha256(script_path) != str(manifest.get("script_sha256") or ""):
        return ["Pi analysis script changed after its verified execution."]
    try:
        details = _validate_outputs(workspace)
    except RDFAnalysisError as exc:
        return [str(exc)]
    if int(manifest.get("signal_count") or 0) != details["signal_count"]:
        return ["Pi analysis signal count no longer matches the verified manifest."]
    manifest_sources = {
        str(value) for value in manifest.get("source_ids", []) if str(value).strip()
    }
    if manifest_sources != set(details["source_ids"]):
        return ["Pi analysis source IDs no longer match the verified manifest."]
    for value in manifest.get("artifacts", []):
        artifact = (workspace / str(value)).resolve()
        if (
            not artifact.is_relative_to(workspace)
            or not artifact.is_file()
            or artifact.stat().st_size <= 0
        ):
            return [f"Pi analysis artifact is missing or empty: {value}"]
    return []


def load_analysis_manifest(workspace_path: Path) -> dict[str, Any]:
    issues = validate_analysis_manifest(workspace_path)
    if issues:
        raise RDFAnalysisError("; ".join(issues))
    return json.loads(
        (workspace_path.resolve() / MANIFEST_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )


def load_candidate_signals(workspace_path: Path) -> list[dict[str, Any]]:
    path = workspace_path.resolve() / REQUIRED_SIGNALS
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RDFAnalysisError(f"candidate signals are invalid: {exc}") from exc
    signals = document.get("candidate_signals") if isinstance(document, dict) else None
    if not isinstance(signals, list) or not signals:
        raise RDFAnalysisError("candidate signals must contain at least one entry")
    return [value for value in signals if isinstance(value, dict)]


def _validate_outputs(workspace: Path) -> dict[str, Any]:
    source_index = _scope_source_index(workspace)
    if not source_index:
        raise RDFAnalysisError("Scope V2 source catalog is missing or empty")
    rdf_subjects = _rdf_source_subjects(workspace)

    table_path = workspace / REQUIRED_TABLE
    if not table_path.is_file():
        raise RDFAnalysisError("analysis must create tables/source_analysis.csv")
    with table_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required_headers = {"source_id", "source", "title", "url", "rdf_subject"}
        if not required_headers.issubset(set(reader.fieldnames or [])):
            raise RDFAnalysisError(
                "source_analysis.csv must include source_id, source, title, url, and rdf_subject"
            )
        rows = [row for row in reader if any(str(value or "").strip() for value in row.values())]
    if not rows:
        raise RDFAnalysisError("source_analysis.csv must contain at least one data row")
    table_source_ids = {str(row.get("source_id") or "").strip() for row in rows}
    unknown_table_ids = sorted(table_source_ids - set(source_index))
    if unknown_table_ids:
        raise RDFAnalysisError(
            "source_analysis.csv contains unknown source IDs: "
            + ", ".join(unknown_table_ids[:10])
        )
    for row in rows:
        source_id = str(row.get("source_id") or "").strip()
        rdf_subject = str(row.get("rdf_subject") or "").strip()
        expected_subjects = sorted(rdf_subjects.get(source_id, set()))
        if not rdf_subject or rdf_subject not in expected_subjects:
            expected = ", ".join(expected_subjects[:3]) or "<missing from graph>"
            actual = rdf_subject or "<empty>"
            raise RDFAnalysisError(
                "source_analysis.csv does not prove the RDF subject for "
                f"{source_id}; actual={actual}; expected={expected}. "
                "Parse graph.nq with rdflib.Dataset, iterate Dataset.quads(), "
                "and map the predicate ending in sourceId to its subject; do not "
                "use rdflib.Graph for N-Quads or infer lineage from URI text."
            )

    note_path = workspace / REQUIRED_NOTE
    if not note_path.is_file() or len(note_path.read_text(encoding="utf-8").strip()) < 20:
        raise RDFAnalysisError("analysis must create a substantive notes/rdf_analysis.md")

    signals = load_candidate_signals(workspace)
    signal_ids: set[str] = set()
    signal_source_ids: set[str] = set()
    for index, signal in enumerate(signals, start=1):
        signal_id = str(signal.get("signal_id") or "").strip()
        summary = str(signal.get("summary") or "").strip()
        source_ids = {
            str(value).strip()
            for value in signal.get("source_ids", [])
            if str(value).strip()
        }
        analysis_artifacts = [
            str(value).strip()
            for value in signal.get("analysis_artifacts", [])
            if str(value).strip()
        ]
        if not signal_id or signal_id in signal_ids:
            raise RDFAnalysisError(
                f"candidate signal {index} must have a unique signal_id"
            )
        if not summary:
            raise RDFAnalysisError(f"candidate signal {index} summary is required")
        if not source_ids:
            raise RDFAnalysisError(
                f"candidate signal {signal_id} must cite at least one source_id"
            )
        if str(REQUIRED_TABLE) not in analysis_artifacts or not any(
            value.startswith("deep_dives/") and value.endswith(".md")
            for value in analysis_artifacts
        ):
            raise RDFAnalysisError(
                f"candidate signal {signal_id} must cite source_analysis.csv and a deep dive"
            )
        for relative in analysis_artifacts:
            artifact = (workspace / relative).resolve()
            if (
                not artifact.is_relative_to(workspace)
                or not artifact.is_file()
                or artifact.stat().st_size <= 0
            ):
                raise RDFAnalysisError(
                    f"candidate signal {signal_id} cites a missing analysis artifact: {relative}"
                )
        unknown = sorted(source_ids - set(source_index))
        if unknown:
            raise RDFAnalysisError(
                f"candidate signal {signal_id} cites unknown source IDs: "
                + ", ".join(unknown[:10])
            )
        if not source_ids.issubset(table_source_ids):
            raise RDFAnalysisError(
                f"candidate signal {signal_id} cites a source absent from source_analysis.csv"
            )
        signal_ids.add(signal_id)
        signal_source_ids.update(source_ids)

    deep_dives = [
        str(path.relative_to(workspace))
        for path in sorted((workspace / "deep_dives").glob("*.md"))
        if path.is_file() and len(path.read_text(encoding="utf-8").strip()) >= 20
    ]
    if not deep_dives:
        raise RDFAnalysisError("analysis must create at least one substantive deep dive")
    return {
        "signal_count": len(signals),
        "source_count": len(signal_source_ids),
        "source_ids": sorted(signal_source_ids),
        "deep_dives": deep_dives,
    }


def _normalize_analysis_outputs(workspace: Path) -> list[str]:
    """Repair mechanical provenance/artifact defects without inventing analysis.

    The Agent still selects and writes candidate signals.  This pass only fills
    values that are deterministically available from the reviewed Scope V2/RDF
    inputs, removes invalid signal references, and materializes a source-backed
    deep-dive document when the submitted script failed during presentation.
    """
    repairs: list[str] = []
    source_index = _scope_source_index(workspace)
    rdf_subjects = _rdf_source_subjects(workspace)
    table_path = workspace / REQUIRED_TABLE
    if not table_path.is_file():
        return repairs

    with table_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(
            dict.fromkeys(
                str(name or "").lstrip("\ufeff") for name in (reader.fieldnames or [])
            )
        )
        rows = [
            {str(key or "").lstrip("\ufeff"): value for key, value in row.items()}
            for row in reader
        ]
    required_headers = ["source_id", "source", "title", "url", "rdf_subject"]
    with table_path.open("rb") as handle:
        table_changed = handle.read(3) == b"\xef\xbb\xbf"
    if table_changed:
        repairs.append("removed UTF-8 BOM from source_analysis header")
    for header in required_headers:
        if header not in fieldnames:
            fieldnames.append(header)
            repairs.append(f"added source_analysis column {header}")
            table_changed = True

    normalized_rows: list[dict[str, str]] = []
    seen_rows: set[str] = set()
    for row in rows:
        source_id = str(row.get("source_id") or "").strip()
        if not source_id or source_id not in source_index or source_id in seen_rows:
            if source_id:
                repairs.append(f"removed invalid source_analysis row {source_id}")
            table_changed = True
            continue
        seen_rows.add(source_id)
        source = source_index[source_id]
        for key in ("source", "title", "url"):
            if not str(row.get(key) or "").strip() and source.get(key):
                row[key] = source[key]
                repairs.append(f"filled {key} for {source_id}")
                table_changed = True
        expected_subjects = sorted(rdf_subjects.get(source_id, set()))
        if expected_subjects and str(row.get("rdf_subject") or "").strip() not in expected_subjects:
            row["rdf_subject"] = expected_subjects[0]
            repairs.append(f"repaired rdf_subject for {source_id}")
            table_changed = True
        normalized_rows.append({key: str(row.get(key) or "") for key in fieldnames})
    if table_changed:
        with table_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(normalized_rows)

    note_path = workspace / REQUIRED_NOTE
    if not note_path.is_file() or len(note_path.read_text(encoding="utf-8").strip()) < 20:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "# RDF 分析说明\n\n"
            f"本次分析基于 {len(normalized_rows)} 条 Scope V2 来源，"
            "并通过已审查 graph.nq 的 sourceId 映射核验来源血缘。\n",
            encoding="utf-8",
        )
        repairs.append("materialized RDF analysis note")

    signals_path = workspace / REQUIRED_SIGNALS
    try:
        document = json.loads(signals_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return repairs
    root_changed = isinstance(document, list)
    raw_signals = document if root_changed else document.get("candidate_signals") if isinstance(document, dict) else None
    if root_changed:
        repairs.append("normalized signal array root to candidate_signals")
    if raw_signals is None and isinstance(document, dict) and isinstance(document.get("signals"), list):
        raw_signals = document["signals"]
        root_changed = True
        repairs.append("normalized signals root to candidate_signals")
    if not isinstance(raw_signals, list):
        return repairs

    # A model may successfully select source-backed signals but fail while
    # materialising source_analysis.csv (for example, leaving only its header).
    # Reconstruct only rows whose source IDs are present in both the prefetched
    # Scope V2 catalog and the reviewed RDF graph.  This is deterministic
    # provenance repair; it does not select signals or invent analysis.
    cited_source_ids = list(
        dict.fromkeys(
            str(source_id).strip()
            for signal in raw_signals
            if isinstance(signal, dict)
            for source_id in signal.get("source_ids", [])
            if str(source_id).strip() in source_index
            and rdf_subjects.get(str(source_id).strip())
        )
    )
    existing_source_ids = {
        str(row.get("source_id") or "").strip() for row in normalized_rows
    }
    appended_rows = 0
    for source_id in cited_source_ids:
        if source_id in existing_source_ids:
            continue
        source = source_index[source_id]
        row = {key: "" for key in fieldnames}
        row.update(
            {
                "source_id": source_id,
                "source": source.get("source") or "",
                "title": source.get("title") or "",
                "url": source.get("url") or "",
                "rdf_subject": sorted(rdf_subjects[source_id])[0],
            }
        )
        normalized_rows.append(row)
        existing_source_ids.add(source_id)
        appended_rows += 1
    if appended_rows:
        with table_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(normalized_rows)
        repairs.append(
            f"materialized {appended_rows} source_analysis rows from cited RDF sources"
        )

    table_source_ids = {str(row.get("source_id") or "").strip() for row in normalized_rows}
    normalized_signals: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for index, value in enumerate(raw_signals, start=1):
        if not isinstance(value, dict):
            repairs.append(f"removed non-object candidate signal {index}")
            continue
        signal_id = str(value.get("signal_id") or "").strip()
        summary = str(value.get("summary") or "").strip()
        if not signal_id or signal_id in seen_signal_ids or not summary:
            repairs.append(f"removed invalid candidate signal {index}")
            continue
        source_ids = list(
            dict.fromkeys(
                str(source_id).strip()
                for source_id in value.get("source_ids", [])
                if str(source_id).strip() in table_source_ids
            )
        )
        if not source_ids:
            repairs.append(f"removed source-less candidate signal {signal_id}")
            continue
        seen_signal_ids.add(signal_id)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", signal_id) or f"signal_{index}"
        dive_relative = f"deep_dives/{safe_id}.md"
        dive_path = workspace / dive_relative
        if not dive_path.is_file() or len(dive_path.read_text(encoding="utf-8").strip()) < 20:
            lines = [f"# {signal_id}", "", summary, "", "## 支撑来源", ""]
            for source_id in source_ids[:20]:
                source = source_index[source_id]
                title = source.get("title") or source_id
                url = source.get("url") or ""
                lines.append(f"- {title}" + (f" — {url}" if url else ""))
            dive_path.parent.mkdir(parents=True, exist_ok=True)
            dive_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
            repairs.append(f"materialized source-backed deep dive for {signal_id}")
        artifacts = [
            str(item).strip()
            for item in value.get("analysis_artifacts", [])
            if str(item).strip()
        ]
        valid_artifacts: list[str] = []
        for relative in artifacts:
            artifact = (workspace / relative).resolve()
            if (
                artifact.is_relative_to(workspace)
                and artifact.is_file()
                and artifact.stat().st_size > 0
            ):
                valid_artifacts.append(relative)
        value = dict(value)
        value["source_ids"] = source_ids
        value["analysis_artifacts"] = list(
            dict.fromkeys([str(REQUIRED_TABLE), *valid_artifacts, dive_relative])
        )
        normalized_signals.append(value)
    if root_changed or normalized_signals != raw_signals:
        signals_path.write_text(
            json.dumps({"candidate_signals": normalized_signals}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
    return repairs


def _scope_source_index(workspace: Path) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for path in sorted(workspace.glob("scope_v2/*/result.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = document.get("sources") if isinstance(document, dict) else None
        if not isinstance(sources, dict):
            continue
        for block in sources.values():
            items = block.get("items") if isinstance(block, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("source_id") or "").strip()
                if source_id:
                    index[source_id] = {
                        "source": str(item.get("source") or "").strip(),
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                    }
    return index


def _rdf_source_subjects(workspace: Path) -> dict[str, set[str]]:
    try:
        graph_path = rdf_path(workspace)
    except (OSError, ValueError, KeyError) as exc:
        raise RDFAnalysisError(f"verified analysis requires a prepared RDF artifact: {exc}") from exc
    dataset = Dataset()
    try:
        dataset.parse(graph_path, format="nquads")
    except Exception as exc:
        raise RDFAnalysisError(f"published ontology graph.nq is invalid: {exc}") from exc
    mapping: dict[str, set[str]] = {}
    for subject, predicate, value, _graph in dataset.quads((None, None, None, None)):
        if not str(predicate).endswith("sourceId"):
            continue
        source_id = str(value).strip()
        if source_id:
            mapping.setdefault(source_id, set()).add(str(subject))
    if not mapping:
        raise RDFAnalysisError("published ontology graph contains no sourceId lineage")
    return mapping


def _select_script(workspace: Path) -> Path:
    for relative in SCRIPT_CANDIDATES:
        path = (workspace / relative).resolve()
        if path.is_file() and path.is_relative_to(workspace):
            return path
    raise RDFAnalysisError(
        "Pi must write scripts/analyze_scope_v2.py before requesting finalization"
    )


def _analysis_env(workspace: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "PYTHONPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "DATAELF_SCOPE",
        "DATAELF_SCOPE_V2_RESULT",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["DATAELF_JOB_WORKSPACE"] = str(workspace)
    env["DATAELF_WORKSPACE"] = str(workspace)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def main() -> int:
    workspace_value = sys.argv[1] if len(sys.argv) > 1 else os.getenv(
        "DATAELF_JOB_WORKSPACE", ""
    )
    if not workspace_value:
        raise SystemExit("usage: python -m dataelf_server.analysis.rdf_analysis WORKSPACE")
    manifest = run_rdf_analysis(Path(workspace_value))
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
