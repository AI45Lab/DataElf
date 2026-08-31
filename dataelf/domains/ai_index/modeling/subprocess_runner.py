from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json
from dataelf.config import AIIndexModelingConfig
from dataelf.domains.ai_index.modeling.contracts import (
    AI_INDEX_MODELING_STAGE1_INCOMPLETE,
    AI_INDEX_MODELING_STAGE2_FAILED,
    AI_INDEX_MODELING_RDF_INVALID,
    AI_INDEX_MODELING_SUBPROCESS_FAILED,
    AI_INDEX_MODELING_SUBPROCESS_TIMEOUT,
    OntologyRunResult,
)


def run_ontology_subprocess(
    workspace: Path,
    config: AIIndexModelingConfig,
    runtime_env: dict[str, str],
) -> OntologyRunResult:
    control_dir = workspace / "modeling" / "ai_index"
    logs_dir = workspace / "logs"
    control_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    request_path = control_dir / "worker_request.json"
    result_path = control_dir / "worker_result.json"
    progress_path = control_dir / "worker_progress.json"
    stdout_path = logs_dir / "ai_index_modeling_stdout.log"
    stderr_path = logs_dir / "ai_index_modeling_stderr.log"
    for stale in (result_path, progress_path):
        stale.unlink(missing_ok=True)
    atomic_write_json(
        request_path,
        {"workspace_path": str(workspace.resolve()), "modeling": config.model_dump(mode="json")},
    )
    command = [
        sys.executable,
        "-m",
        "dataelf.domains.ai_index.modeling.worker",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--progress",
        str(progress_path),
    ]
    environment = os.environ.copy()
    environment.update({key: str(value) for key, value in runtime_env.items()})
    repo_root = Path(__file__).resolve().parents[4]
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(repo_root) if not existing_pythonpath else f"{repo_root}{os.pathsep}{existing_pythonpath}"
    timeout = config.stage1_process_timeout_seconds + config.stage2_total_timeout_seconds + 120
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process_group(process)
        stdout_path.write_text(stdout or "", encoding="utf-8")
        stderr_path.write_text((stderr or "") + f"\nModeling subprocess timed out after {timeout} seconds.\n", encoding="utf-8")
        return _subprocess_failure(progress_path, AI_INDEX_MODELING_SUBPROCESS_TIMEOUT, "AI Index modeling subprocess timed out.")
    except KeyboardInterrupt:
        _terminate_process_group(process)
        raise
    stdout_path.write_text(stdout or "", encoding="utf-8")
    stderr_path.write_text(stderr or "", encoding="utf-8")
    if result_path.is_file():
        try:
            return OntologyRunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if process.returncode != 0:
        return _subprocess_failure(
            progress_path,
            AI_INDEX_MODELING_SUBPROCESS_FAILED,
            f"AI Index modeling subprocess exited with code {process.returncode}; see {stderr_path}.",
        )
    return _subprocess_failure(
        progress_path,
        AI_INDEX_MODELING_SUBPROCESS_FAILED,
        "AI Index modeling subprocess completed without a valid result contract.",
    )


def _subprocess_failure(progress_path: Path, fallback_code: str, message: str) -> OntologyRunResult:
    stage = "stage1"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress.get("stage") in {"stage1", "stage2", "rdf"}:
            stage = str(progress["stage"])
    except (OSError, json.JSONDecodeError):
        pass
    progress_exists = progress_path.is_file()
    if progress_exists and stage == "stage1":
        code = AI_INDEX_MODELING_STAGE1_INCOMPLETE
    elif progress_exists and stage == "stage2":
        code = AI_INDEX_MODELING_STAGE2_FAILED
    elif progress_exists and stage == "rdf":
        code = AI_INDEX_MODELING_RDF_INVALID
    else:
        code = fallback_code
    return OntologyRunResult(status="failed", stage=stage, error_code=code, error_message=message)


def _terminate_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        return process.communicate()


__all__ = ["run_ontology_subprocess"]
