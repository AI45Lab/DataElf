from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from dataelf.config import DataElfConfig
from dataelf.discovery.artifacts import ArtifactContractError, validate_outputs, validate_stage_artifacts, write_artifact_manifest
from dataelf.discovery.contracts import (
    ArtifactRef,
    DiscoveryContext,
    DiscoveryJob,
    DomainPlugin,
    JobSpec,
    ReviewResult,
)
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.explorer_factory import create_explorer
from dataelf.discovery.prompt_builder import write_discovery_prompt
from dataelf.discovery.workspace import prepare_workspace
from dataelf.schemas import new_id, now_utc
from dataelf.stores.sqlite_store import SQLiteStore


logger = logging.getLogger("dataelf.discovery")


class StoreLike(Protocol):
    def save_discovery_job(self, job: DiscoveryJob) -> None: ...
    def add_trace_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> str: ...
    def save_quality_review(self, review: ReviewResult) -> None: ...


class NullStore:
    def save_discovery_job(self, job: DiscoveryJob) -> None:
        return None

    def add_trace_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> str:
        logger.debug("trace skipped: %s %s %s", job_id, event_type, payload)
        return ""

    def save_quality_review(self, review: ReviewResult) -> None:
        return None


def run_discovery(user_query: str, config: DataElfConfig) -> DiscoveryJob:
    return run_job(JobSpec(domain="ai_index", objective=user_query), config)


def run_job(spec: JobSpec, config: DataElfConfig, registry: DomainRegistry | None = None) -> DiscoveryJob:
    config.ensure_dirs()
    store = _create_store(config)
    plugin = (registry or DomainRegistry()).load_plugin(spec.domain, config)
    spec = plugin.normalize_spec(spec)
    job = _initialize_job(spec, config, store)
    workspace = prepare_workspace(Path(job.workspace_path), spec)
    job.artifacts.append(ArtifactRef(
        artifact_id="job_spec", kind="job_spec", path="job_spec.json", role="input",
        producer_stage="core", media_type="application/json",
    ))
    context = DiscoveryContext(
        workspace_path=str(workspace), spec=spec, manifest=plugin.manifest,
        model=config.explorer.pi.model, env=dict(config.env),
    )

    preparation = plugin.prepare(spec, str(workspace), config)
    _trace_stage(store, job, "domain_prepare", preparation)
    if preparation.status != "completed":
        return _fail(job, store, workspace, "domain_prepare", preparation.error_code, preparation.error_message)
    try:
        validate_stage_artifacts(workspace, preparation.artifacts)
    except ArtifactContractError as exc:
        return _fail(job, store, workspace, "domain_prepare", "STAGE_ARTIFACT_INVALID", str(exc))
    job.artifacts.extend(preparation.artifacts)
    context = context.model_copy(update={
        "domain_context": preparation.context,
        "env": {**context.env, **preparation.env},
        "artifacts": list(job.artifacts),
    })

    modeler = plugin.create_modeler(spec, config)
    if modeler is not None:
        modeling = modeler.run(job, context)
        _trace_stage(store, job, "domain_modeling", modeling)
        if modeling.status != "completed":
            return _fail(job, store, workspace, "domain_modeling", modeling.error_code, modeling.error_message)
        try:
            validate_stage_artifacts(workspace, modeling.artifacts)
        except ArtifactContractError as exc:
            return _fail(job, store, workspace, "domain_modeling", "STAGE_ARTIFACT_INVALID", str(exc))
        job.artifacts.extend(modeling.artifacts)
        context = context.model_copy(update={
            "domain_context": {**context.domain_context, **modeling.context},
            "env": {**context.env, **modeling.env},
            "artifacts": list(job.artifacts),
        })

    contract = plugin.output_contract(spec)
    prompt_path = write_discovery_prompt(job, context, plugin.build_prompt(job, context), contract)
    job.artifacts.append(ArtifactRef(
        artifact_id="discovery_prompt", kind="prompt", path="prompts/discovery_prompt.md",
        role="input", producer_stage="prompt_composer", media_type="text/markdown",
    ))
    context = context.model_copy(update={"prompt_path": str(prompt_path)})
    explorer = create_explorer(config)
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


def _create_store(config: DataElfConfig) -> StoreLike:
    if not config.runtime.enable_sqlite:
        return NullStore()
    store = SQLiteStore(config.runtime.sqlite_path)
    store.init_schema()
    return store


def _initialize_job(spec: JobSpec, config: DataElfConfig, store: StoreLike) -> DiscoveryJob:
    job_id = new_id("job")
    job = DiscoveryJob(
        job_id=job_id, spec=spec, status="running",
        workspace_path=str(config.runtime.workspaces_dir / job_id),
    )
    store.save_discovery_job(job)
    store.add_trace_event(job.job_id, "job_initialized", {
        "domain": spec.domain, "objective": spec.objective, "workspace_path": job.workspace_path,
    })
    return job


def _trace_stage(store: StoreLike, job: DiscoveryJob, stage: str, result: Any) -> None:
    store.add_trace_event(job.job_id, f"{stage}_completed", result.model_dump(mode="json"))


def _fail(
    job: DiscoveryJob,
    store: StoreLike,
    workspace: Path,
    stage: str,
    error_code: str | None,
    error_message: str | None,
) -> DiscoveryJob:
    code = error_code or f"{stage.upper()}_FAILED"
    message = error_message or f"{stage} failed."
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
    store: StoreLike,
    workspace: Path,
    plugin: DomainPlugin | None,
    review: ReviewResult,
    error_code: str | None = None,
    error_message: str | None = None,
) -> DiscoveryJob:
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


__all__ = ["NullStore", "run_discovery", "run_job"]
