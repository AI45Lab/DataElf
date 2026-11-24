"""Domain-owned output semantics for reviewed AI Index research."""

import json
from pathlib import Path
from dataelf.discovery.artifacts import (
    validate_outputs,
    resolve_workspace_path,
    ArtifactContractError,
)


def source_references(workspace):
    refs = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key == "id"
                    or key.endswith("_id")
                    or key
                    in {
                        "url",
                        "source_url",
                        "homepage",
                        "landing_page_url",
                        "pdf_url",
                        "link",
                        "html_url",
                    }
                ) and isinstance(item, (str, int)):
                    refs.add(str(item))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for path in (workspace / "raw").rglob("*.json"):
        refs.add(path.relative_to(workspace).as_posix())
        try:
            visit(json.loads(path.read_text()))
        except (ValueError, UnicodeError):
            pass
    return refs


def locator_exists(workspace, locator, refs):
    if locator in refs:
        return True
    relative, _, pointer = locator.partition("#")
    try:
        path = resolve_workspace_path(workspace, relative)
    except ArtifactContractError:
        return False
    if not path.is_file():
        return False
    if pointer:
        try:
            value = json.loads(path.read_text())
            if not pointer.startswith("/"):
                return False
            for token in pointer[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
        except (ValueError, KeyError, IndexError, TypeError):
            return False
    return True


def validate_submission(workspace, contract, spec):
    workspace = Path(workspace).resolve()
    outputs, warnings = validate_outputs(workspace, contract)
    signals = json.loads((workspace / "insights/candidate_signals.json").read_text())[
        "candidate_signals"
    ]
    ids = []
    for index, signal in enumerate(signals):
        if (
            not isinstance(signal, dict)
            or not isinstance(signal.get("signal_id"), str)
            or not signal["signal_id"].strip()
        ):
            raise ArtifactContractError(
                f"candidate_signals[{index}].signal_id must be nonempty"
            )
        if not isinstance(signal.get("summary"), str) or not signal["summary"].strip():
            raise ArtifactContractError(
                f"candidate_signals[{index}].summary must contain the actual signal"
            )
        ids.append(signal["signal_id"])
    if len(set(ids)) != len(ids):
        raise ArtifactContractError("candidate_signals signal_id values must be unique")
    insights = json.loads((workspace / "insights/insight_candidates.json").read_text())[
        "insight_candidates"
    ]
    refs = source_references(workspace)
    maximum = spec["parameters"].get("expected_outputs")
    if maximum is not None and len(insights) > int(maximum):
        raise ArtifactContractError(
            "insight_candidates exceeds the user requested maximum"
        )
    insight_ids = []
    for index, item in enumerate(insights):
        insight_ids.append(item["insight_id"])
        for ident in item["supporting_signals"]:
            if ident not in ids:
                raise ArtifactContractError(
                    f"insight_candidates[{index}].supporting_signals references unknown signal {ident}"
                )
        for artifact in item["analysis_artifacts"]:
            relative = artifact if isinstance(artifact, str) else artifact["path"]
            path = resolve_workspace_path(workspace, relative)
            if not path.is_file() or not path.stat().st_size:
                raise ArtifactContractError(
                    f"insight_candidates[{index}].analysis_artifacts missing file: {relative}"
                )
        for support_index, support in enumerate(item["external_support"]):
            locators = (
                [support]
                if isinstance(support, str)
                else [
                    str(value)
                    for key, value in support.items()
                    if value
                    and (
                        key in {"path", "id", "url", "source_url", "link", "html_url"}
                        or key.endswith("_id")
                    )
                ]
            )
            if not locators or not any(
                locator_exists(workspace, loc, refs) for loc in locators
            ):
                raise ArtifactContractError(
                    f"insight_candidates[{index}].external_support[{support_index}] needs an exact "
                    "original ID, registered URL or workspace file pointer. Use a bare string "
                    'or {"path":"raw/.../response.json#/data/list/0","title":"description"}; '
                    "keep descriptions separate from the locator."
                )
    if len(set(insight_ids)) != len(insight_ids):
        raise ArtifactContractError("insight_id values must be unique")
    return outputs, warnings
