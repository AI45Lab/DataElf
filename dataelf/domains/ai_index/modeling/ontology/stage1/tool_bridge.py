#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[6]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import read_json_object  # noqa: E402
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.source import query_evidence  # noqa: E402


TOOL_MAP = {
    "ontology_source_overview": "source_overview",
    "ontology_describe_table": "describe_table",
    "ontology_sample_rows": "sample_rows",
    "ontology_profile_columns": "profile_columns",
    "ontology_profile_identity": "profile_identity",
    "ontology_validate_join": "validate_join",
    "ontology_relationship_candidates": "relationship_candidates",
    "ontology_describe_raw_endpoint": "describe_raw_endpoint",
    "ontology_profile_raw_path": "profile_raw_path",
    "ontology_trace_normalized_column": "trace_normalized_column",
    "ontology_compare_relation_sources": "compare_relation_sources",
    "ontology_replay_source": "replay_source",
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--tool", required=True, choices=sorted(TOOL_MAP))
    return parser.parse_args()


def _normalize(tool: str, value: dict[str, Any]) -> dict[str, Any]:
    if tool == "ontology_profile_identity":
        return {"table": value.get("table"), "columns": value.get("columns", [])}
    if tool == "ontology_validate_join":
        return {
            "sourceTable": value.get("source_table"),
            "sourceColumns": value.get("source_columns", []),
            "targetTable": value.get("target_table"),
            "targetColumns": value.get("target_columns", []),
        }
    if tool == "ontology_profile_raw_path":
        return {"endpoint": value.get("endpoint", ""), "pathPattern": value.get("path_pattern", "")}
    return value


def main() -> int:
    args = _arguments()
    try:
        request = json.loads(sys.stdin.read() or "{}")
        if not isinstance(request, dict):
            raise ValueError("tool arguments must be a JSON object")
        bundle = read_json_object(args.evidence)
        result = query_evidence(bundle, TOOL_MAP[args.tool], _normalize(args.tool, request))
        envelope = {"isError": False, "result": result}
    except Exception as exc:  # The runtime needs a bounded structured tool error.
        envelope = {"isError": True, "result": {"error": type(exc).__name__, "message": str(exc)[:2000]}}
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
