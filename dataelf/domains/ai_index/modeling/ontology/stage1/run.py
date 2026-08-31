#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[6]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import read_json_object  # noqa: E402
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import list_checkpoints, stage1_root  # noqa: E402
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import load_config  # noqa: E402
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.pipeline import (  # noqa: E402
    apply_manual_audit,
    generate_pipeline,
    validate_published_bundle,
)


DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DataElf Ontology Stage 1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate, review, repair, and conditionally publish an ontology")
    generate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    generate.add_argument("--workspace", type=Path, required=True)
    resume_group = generate.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", nargs="?", const="auto", default="auto", metavar="RUN_ID")
    resume_group.add_argument("--no-resume", action="store_true")
    generate.add_argument("--repair-from", metavar="RUN_ID")

    status = subparsers.add_parser("status", help="show Stage 1 runs for a workspace")
    status.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    status.add_argument("--workspace", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="offline validation of a published bundle")
    validate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    validate.add_argument("--bundle", type=Path, required=True)

    audit = subparsers.add_parser("audit", help="apply the one-time Codex audit decision")
    audit.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    audit.add_argument("--workspace", type=Path, required=True)
    audit.add_argument("--run-id", required=True)
    audit.add_argument("--report", type=Path, required=True)
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    args = _parser().parse_args()
    from dataelf.domains.ai_index.modeling.ontology_adapter import AI_INDEX_ONTOLOGY_ADAPTER

    if args.command == "generate":
        config = load_config(args.config)
        resume = None if args.no_resume else args.resume
        result = generate_pipeline(
            config=config,
            workspace=args.workspace,
            resume=resume,
            repair_from=args.repair_from,
            domain_adapter=AI_INDEX_ONTOLOGY_ADAPTER,
        )
        _print(result)
        return 0 if result.get("status") in {"completed", "awaiting_manual_audit", "paused_timeout", "paused_runtime_error"} else 2
    if args.command == "status":
        from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import configure_artifact_subdir

        configure_artifact_subdir(load_config(args.config))
        workspace = args.workspace.resolve()
        latest_path = stage1_root(workspace) / "latest.json"
        result = {
            "workspace": str(workspace),
            "latest": read_json_object(latest_path) if latest_path.is_file() else None,
            "runs": [
                {
                    "runId": item.get("runId"),
                    "stage": item.get("stage"),
                    "round": item.get("round"),
                    "updatedAt": item.get("updatedAt"),
                    "reason": item.get("reason"),
                }
                for item in list_checkpoints(workspace)
            ],
        }
        _print(result)
        return 0
    if args.command == "validate":
        result = validate_published_bundle(
            args.bundle,
            load_config(args.config),
            domain_adapter=AI_INDEX_ONTOLOGY_ADAPTER,
        )
        _print(result)
        return 0 if result["status"] == "valid" else 2
    if args.command == "audit":
        result = apply_manual_audit(
            config=load_config(args.config),
            workspace=args.workspace,
            run_id=args.run_id,
            report=read_json_object(args.report),
            domain_adapter=AI_INDEX_ONTOLOGY_ADAPTER,
        )
        _print(result)
        return 0 if result["status"] == "completed" else 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
