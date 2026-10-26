"""Stable legacy behavior migrated from DataElf 78f2ef4.

Owned by legacy entries; new AI research must not add behavior here.
Repository license applies. Only package-root paths and boundary types adapt.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from dataelf.discovery.artifacts import ArtifactContractError, validate_outputs, validate_stage_artifacts, write_artifact_manifest
from dataelf.discovery.contracts import ArtifactRef, DiscoveryJob, DomainPlugin, ReviewResult
from dataelf.discovery.redaction import redact_data, redact_text, secret_values
from dataelf.discovery.run_control import check_cancelled, current_run
from dataelf.schemas import new_id, now_utc


def _stage(name: str) -> None:
    check_cancelled()
    control = current_run()
    if control:
        control.emit(name)

def _trace_stage(store: Any, job: DiscoveryJob, stage: str, result: Any) -> None:
    control = current_run()
    payload = result.model_dump(mode="json")
    has_environment = "env" in payload
    environment = payload.pop("env", {})
    secrets = secret_values(environment)
    if control:
        control.secrets.update(secrets)
        secrets = control.secrets
    payload = redact_data(payload, secrets)
    # Preserve main's presence-only database trace without persisting values.
    if has_environment:
        payload["env"] = {key: bool(value) for key, value in environment.items()}
    store.add_trace_event(job.job_id, f"{stage}_completed", payload)
    if control:
        # The workspace trace additionally excludes the environment field.
        control.record(f"{stage}_completed", payload)

def _fail(
    job: DiscoveryJob,
    store: Any,
    workspace: Path,
    stage: str,
    error_code: str | None,
    error_message: str | None,
) -> DiscoveryJob:
    code = error_code or f"{stage.upper()}_FAILED"
    control = current_run()
    message = redact_text(error_message or f"{stage} failed.", control.secrets if control else ())
    review = ReviewResult(
        review_id=new_id("review"), job_id=job.job_id, status="skipped",
        warnings=[f"Skipped because {stage} failed ({code}): {message}"],
        metrics={"failed_stage": stage},
    )
    _write_review(workspace, review)
    store.save_quality_review(review)
    return _finalize(job, store, workspace, None, review, code, message)

def _finalize(
    job: DiscoveryJob,
    store: Any,
    workspace: Path,
    plugin: DomainPlugin | None,
    review: ReviewResult,
    error_code: str | None = None,
    error_message: str | None = None,
) -> DiscoveryJob:
    if not error_code:
        _stage("finalization")
    # Preserve intermediate evidence, including artifacts written before a
    # component raised. Never inventory a path outside this run's workspace.
    known = {item.path for item in job.artifacts}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace).as_posix()
        if relative in known or relative in {"artifact_manifest.json", "workspace_index.json"}:
            continue
        if not path.resolve().is_relative_to(workspace.resolve()):
            continue
        job.artifacts.append(ArtifactRef(artifact_id=f"workspace_file_{len(job.artifacts)}", kind="workspace_file",
            path=relative, role="evidence", producer_stage="finalization"))
    job.status = "failed" if error_code else "completed"
    job.error_code = error_code
    job.error_message = error_message
    job.updated_at = now_utc()
    write_artifact_manifest(workspace, job.artifacts)
    result_ids = plugin.result_ids(str(workspace)) if plugin and not error_code else []
    index = {
        "job_id": job.job_id,
        "status": job.status,
        "spec": job.spec.model_dump(mode="json"),
        "error": {"code": error_code, "message": error_message} if error_code else None,
        "result_ids": result_ids,
        "review": review.model_dump(mode="json"),
        "artifact_manifest": "artifact_manifest.json",
    }
    (workspace / "workspace_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    store.save_discovery_job(job)
    store.add_trace_event(job.job_id, "job_finalized", {"status": job.status, "error_code": error_code})
    return job

def _write_review(workspace: Path, review: ReviewResult) -> None:
    path = workspace / "reviews" / "quality_review.json"
    path.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")

def execute(job, config, store, workspace, plugin, context, explorer, contract):
    explorer_result = explorer.run(job, context)
    job.artifacts.extend(explorer_result.artifacts)
    try:
        validate_stage_artifacts(workspace, explorer_result.artifacts)
    except ArtifactContractError as exc:
        return _fail(job, store, workspace, "explorer", "STAGE_ARTIFACT_INVALID", str(exc))
    _trace_stage(store, job, "explorer", explorer_result)
    if explorer_result.status != "completed":
        return _fail(
            job, store, workspace, "explorer", explorer_result.error_code,
            explorer_result.error_message or "Pi explorer failed.",
        )

    _stage("output_validation")
    try:
        outputs, contract_warnings = validate_outputs(workspace, contract)
    except ArtifactContractError as exc:
        return _fail(job, store, workspace, "output_validation", "OUTPUT_CONTRACT_FAILED", str(exc))
    job.artifacts.extend(outputs)
    store.add_trace_event(job.job_id, "outputs_validated", {
        "contract_id": contract.contract_id,
        "artifact_ids": [item.artifact_id for item in outputs],
        "warnings": contract_warnings,
    })

    _stage("domain_review")
    review = plugin.review(job, str(workspace))
    if contract_warnings:
        review.warnings.extend(contract_warnings)
        if review.status == "pass":
            review.status = "pass_with_warnings"
        review.recommended_revision = True
    _write_review(workspace, review)
    store.save_quality_review(review)
    store.add_trace_event(job.job_id, "domain_review_completed", review.model_dump(mode="json"))
    if review.status == "failed":
        return _finalize(job, store, workspace, plugin, review, "DOMAIN_REVIEW_FAILED", "Domain review failed.")
    return _finalize(job, store, workspace, plugin, review)
