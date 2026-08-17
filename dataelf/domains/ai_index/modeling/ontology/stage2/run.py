#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[6]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.compiler import compile_plan, stage2_root  # noqa: E402
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import load_config  # noqa: E402
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.pipeline import (  # noqa: E402
    build,
    list_runs,
    record_codex_audit,
    validate_published,
)


DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"


def _source_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--stage1-bundle", type=Path)
    command.add_argument("--allow-draft", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DataElf raw-JSON Ontology Stage 2")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("compile", "build", "resume", "status", "validate", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        command.add_argument("--workspace", type=Path, required=True)
        if name in {"compile", "build"}:
            _source_options(command)
        if name == "compile":
            command.add_argument("--replace-plan", action="store_true")
        if name == "resume":
            command.add_argument("--run-id")
        if name == "validate":
            command.add_argument("--bundle", type=Path)
        if name == "audit":
            command.add_argument("--run-id", required=True)
            command.add_argument("--decision", choices=("approve", "revise"), required=True)
            command.add_argument("--summary", required=True)
            command.add_argument("--findings", type=Path)
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    args = _parser().parse_args()
    config = load_config(args.config)
    workspace = args.workspace.resolve()
    if args.command == "compile":
        _print(
            compile_plan(
                config,
                workspace,
                replace=args.replace_plan,
                stage1_bundle=args.stage1_bundle,
                allow_draft=args.allow_draft,
            )
        )
        return 0
    if args.command == "build":
        result = build(
            config,
            workspace,
            stage1_bundle=args.stage1_bundle,
            allow_draft=args.allow_draft,
        )
        _print(result)
        return 0 if result.get("status") in {"completed", "candidate_approved", "awaiting_manual_audit"} else 2
    if args.command == "resume":
        run_id = args.run_id
        if not run_id:
            resumable = [item for item in list_runs(workspace, config) if item.get("stage") not in {"completed", "candidate_approved", "terminal_failed", "manual_revise"}]
            if not resumable:
                raise SystemExit("no resumable Stage 2 run")
            run_id = str(resumable[0]["runId"])
        result = build(config, workspace, resume_run_id=run_id)
        _print(result)
        return 0 if result.get("status") in {"completed", "candidate_approved", "awaiting_manual_audit"} else 2
    if args.command == "status":
        latest_path = stage2_root(workspace, config) / "latest.json"
        _print(
            {
                "workspace": str(workspace),
                "latest": json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.is_file() else None,
                "runs": list_runs(workspace, config),
            }
        )
        return 0
    if args.command == "validate":
        result = validate_published(config, workspace, args.bundle)
        _print(result)
        return 0 if result["status"] == "valid" else 2
    if args.command == "audit":
        findings: list[dict[str, object]] = []
        if args.findings:
            value = json.loads(args.findings.read_text(encoding="utf-8"))
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise SystemExit("--findings must contain a JSON array of objects")
            findings = value
        result = record_codex_audit(
            config,
            workspace,
            run_id=args.run_id,
            decision=args.decision,
            summary=args.summary,
            findings=findings,
        )
        _print(result)
        return 0 if result["status"] in {"completed", "candidate_approved"} else 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
