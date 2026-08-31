from __future__ import annotations

import argparse
import signal
from pathlib import Path

from pydantic import BaseModel

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, read_json_object
from dataelf.config import AIIndexModelingConfig
from dataelf.domains.ai_index.modeling.contracts import OntologyRunResult
from dataelf.domains.ai_index.modeling.ontology_runner import AIIndexOntologyRunner


class WorkerRequest(BaseModel):
    workspace_path: str
    modeling: AIIndexModelingConfig


def execute(request_path: Path, result_path: Path, progress_path: Path) -> OntologyRunResult:
    request = WorkerRequest.model_validate(read_json_object(request_path))
    config = request.modeling

    def progress(stage: str) -> None:
        atomic_write_json(progress_path, {"stage": stage})

    runner = AIIndexOntologyRunner(
        config.stage1_config,
        config.stage2_config,
        ontology_template=config.ontology_template,
        model_name=config.model_name,
        model_max_tokens=config.model_max_tokens,
        stage1_process_timeout_seconds=config.stage1_process_timeout_seconds,
        stage1_request_timeout_seconds=config.stage1_request_timeout_seconds,
        stage1_request_max_retries=config.stage1_request_max_retries,
        stage2_request_timeout_seconds=config.stage2_request_timeout_seconds,
        stage2_request_max_retries=config.stage2_request_max_retries,
        stage2_total_timeout_seconds=config.stage2_total_timeout_seconds,
        progress=progress,
    )
    result = runner.run(Path(request.workspace_path))
    payload = result.model_dump(mode="json")
    payload["details"] = {}
    atomic_write_json(result_path, payload)
    return result


def _interrupt_worker(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _interrupt_worker)
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    args = parser.parse_args()
    result = execute(args.request, args.result, args.progress)
    raise SystemExit(0 if result.status == "completed" else 2)


if __name__ == "__main__":
    main()
