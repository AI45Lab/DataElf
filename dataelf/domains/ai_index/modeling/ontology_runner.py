from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

from dataelf.domains.ai_index.modeling.contracts import (
    AI_INDEX_MODELING_RDF_INVALID,
    AI_INDEX_MODELING_STAGE1_FAILED,
    AI_INDEX_MODELING_STAGE1_INCOMPLETE,
    AI_INDEX_MODELING_STAGE2_FAILED,
    AI_INDEX_MODELING_STAGE2_INCOMPATIBLE,
    OntologyRunResult,
)


class AIIndexOntologyRunner:
    """Run the AI Index-owned ontology implementation."""

    def __init__(
        self,
        stage1_config_path: str | Path,
        stage2_config_path: str | Path,
        *,
        ontology_template: str | None = None,
        model_name: str | None = None,
        model_max_tokens: int | None = None,
        stage1_process_timeout_seconds: int = 7200,
        stage1_request_timeout_seconds: int = 900,
        stage1_request_max_retries: int = 3,
        stage2_request_timeout_seconds: int = 600,
        stage2_request_max_retries: int = 3,
        stage2_total_timeout_seconds: int = 1800,
        progress: Callable[[str], None] | None = None,
    ):
        self.stage1_config_path = _config_path(stage1_config_path)
        self.stage2_config_path = _config_path(stage2_config_path)
        self.ontology_template = ontology_template.strip() if ontology_template and ontology_template.strip() else None
        self.model_name = model_name.strip() if model_name and model_name.strip() else None
        self.model_max_tokens = model_max_tokens
        self.stage1_process_timeout_seconds = stage1_process_timeout_seconds
        self.stage1_request_timeout_seconds = stage1_request_timeout_seconds
        self.stage1_request_max_retries = stage1_request_max_retries
        self.stage2_request_timeout_seconds = stage2_request_timeout_seconds
        self.stage2_request_max_retries = stage2_request_max_retries
        self.stage2_total_timeout_seconds = stage2_total_timeout_seconds
        self.progress = progress or (lambda _stage: None)

    def run(self, workspace_path: Path) -> OntologyRunResult:
        workspace = workspace_path.resolve()
        self.progress("stage1")
        try:
            from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import load_config as load_stage1_config
            from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.pipeline import validate_published_bundle

            stage1_config = _with_model_overrides(
                load_stage1_config(self.stage1_config_path),
                fields=("generator", "reviewer"),
                name=self.model_name,
                max_tokens=self.model_max_tokens,
                model_updates={
                    "process_timeout_seconds": self.stage1_process_timeout_seconds,
                    "request_timeout_seconds": self.stage1_request_timeout_seconds,
                    "request_max_retries": self.stage1_request_max_retries,
                },
            )
            if self.ontology_template:
                from dataelf.domains.ai_index.modeling.template import bind_template

                stage1 = bind_template(stage1_config, workspace, self.ontology_template)
                stage1_attempts = 0
            else:
                from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.pipeline import generate_pipeline
                from dataelf.domains.ai_index.modeling.ontology_adapter import AI_INDEX_ONTOLOGY_ADAPTER

                stage1 = generate_pipeline(
                    config=stage1_config,
                    workspace=workspace,
                    resume=None,
                    repair_from=None,
                    domain_adapter=AI_INDEX_ONTOLOGY_ADAPTER,
                )
                stage1_attempts = 1
        except Exception as exc:
            return OntologyRunResult(
                status="failed",
                stage="stage1",
                error_code=AI_INDEX_MODELING_STAGE1_FAILED,
                error_message=str(exc),
            )

        stage1_status = str(stage1.get("status", "failed"))
        stage1_run_id = _optional_string(stage1.get("runId"))
        if stage1_status != "completed":
            incomplete = stage1_status in {
                "awaiting_manual_audit",
                "paused_timeout",
                "paused_interrupted",
                "paused_runtime_error",
            }
            return OntologyRunResult(
                status="incomplete" if incomplete else "failed",
                stage="stage1",
                stage1_run_id=stage1_run_id,
                stage1_bundle=_optional_string(stage1.get("bundle")),
                error_code=AI_INDEX_MODELING_STAGE1_INCOMPLETE if incomplete else AI_INDEX_MODELING_STAGE1_FAILED,
                error_message=f"Ontology Stage 1 returned status={stage1_status}.",
                details={
                    "stage1": stage1,
                    "stage1Attempts": stage1_attempts,
                    "stage1Mode": "template" if self.ontology_template else "generated",
                },
            )

        stage1_bundle = Path(str(stage1.get("bundle", ""))).resolve()
        try:
            from dataelf.domains.ai_index.modeling.ontology_adapter import AI_INDEX_ONTOLOGY_ADAPTER

            stage1_validation = validate_published_bundle(
                stage1_bundle, stage1_config, workspace, AI_INDEX_ONTOLOGY_ADAPTER
            )
        except Exception as exc:
            return OntologyRunResult(
                status="failed",
                stage="stage1",
                stage1_run_id=stage1_run_id,
                stage1_bundle=str(stage1_bundle),
                error_code=AI_INDEX_MODELING_STAGE1_FAILED,
                error_message=f"Ontology Stage 1 published validation failed: {exc}",
                details={
                    "stage1": stage1,
                    "stage1Attempts": stage1_attempts,
                    "stage1Mode": "template" if self.ontology_template else "generated",
                },
            )
        if stage1_validation.get("status") != "valid":
            return OntologyRunResult(
                status="failed",
                stage="stage1",
                stage1_run_id=stage1_run_id,
                stage1_bundle=str(stage1_bundle),
                error_code=AI_INDEX_MODELING_STAGE1_FAILED,
                error_message="Ontology Stage 1 published bundle is invalid.",
                details={
                    "stage1": stage1,
                    "stage1Attempts": stage1_attempts,
                    "stage1Mode": "template" if self.ontology_template else "generated",
                    "stage1Validation": stage1_validation,
                },
            )

        try:
            self.progress("stage2")
            from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import load_config as load_stage2_config
            from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.pipeline import build, validate_published

            stage2_config = _with_model_overrides(
                load_stage2_config(self.stage2_config_path),
                fields=("compiler", "reviewer"),
                name=self.model_name,
                max_tokens=self.model_max_tokens,
                model_updates={
                    "request_timeout_seconds": self.stage2_request_timeout_seconds,
                    "request_max_retries": self.stage2_request_max_retries,
                },
            )
            stage2_config = replace(
                stage2_config,
                total_stage_timeout_seconds=self.stage2_total_timeout_seconds,
            )
            stage2 = build(stage2_config, workspace, resume_run_id=None)
            stage2_attempts = 1
        except Exception as exc:
            error_code = (
                AI_INDEX_MODELING_STAGE2_INCOMPATIBLE
                if _is_contract_error(exc)
                else AI_INDEX_MODELING_STAGE2_FAILED
            )
            return OntologyRunResult(
                status="failed",
                stage="stage2",
                stage1_run_id=stage1_run_id,
                stage1_bundle=str(stage1_bundle),
                error_code=error_code,
                error_message=str(exc),
                details={
                    "stage1": stage1,
                    "stage1Attempts": stage1_attempts,
                    "stage1Mode": "template" if self.ontology_template else "generated",
                },
            )

        if str(stage2.get("status", "failed")) != "completed":
            return OntologyRunResult(
                status="failed",
                stage="stage2",
                stage1_run_id=stage1_run_id,
                stage2_run_id=_optional_string(stage2.get("runId")),
                stage1_bundle=str(stage1_bundle),
                stage2_bundle=_optional_string(stage2.get("bundle")),
                error_code=AI_INDEX_MODELING_STAGE2_FAILED,
                error_message=f"Ontology Stage 2 returned status={stage2.get('status')}.",
                details={
                    "stage1": stage1,
                    "stage1Attempts": stage1_attempts,
                    "stage1Mode": "template" if self.ontology_template else "generated",
                    "stage2": stage2,
                    "stage2Attempts": stage2_attempts,
                },
            )

        bundle = Path(str(stage2.get("bundle", ""))).resolve()
        rdfxml = Path(str(stage2.get("stableRdf", bundle / stage2_config.output.rdfxml_name))).resolve()
        nquads = (bundle / stage2_config.output.nquads_name).resolve()
        ntriples = (bundle / stage2_config.output.ntriples_name).resolve()
        manifest = (bundle / stage2_config.output.manifest_name).resolve()
        validation = (bundle / stage2_config.output.validation_name).resolve()
        self.progress("rdf")
        try:
            stage2_validation = validate_published(stage2_config, workspace, bundle)
        except Exception as exc:
            stage2_validation = {"status": "invalid", "error": str(exc)}
        invalid_reason = _rdf_invalid_reason(nquads, rdfxml, ntriples, manifest, validation)
        if not invalid_reason and stage2_validation.get("status") != "valid":
            invalid_reason = "Ontology Stage 2 published bundle failed offline validation."
        if invalid_reason:
            return OntologyRunResult(
                status="failed",
                stage="rdf",
                stage1_run_id=stage1_run_id,
                stage2_run_id=_optional_string(stage2.get("runId")),
                stage1_bundle=str(stage1_bundle),
                stage2_bundle=str(bundle),
                nquads_path=str(nquads),
                rdfxml_path=str(rdfxml),
                ntriples_path=str(ntriples),
                manifest_path=str(manifest),
                validation_path=str(validation),
                error_code=AI_INDEX_MODELING_RDF_INVALID,
                error_message=invalid_reason,
                details={
                    "stage1": stage1,
                    "stage1Attempts": stage1_attempts,
                    "stage1Mode": "template" if self.ontology_template else "generated",
                    "stage1Validation": stage1_validation,
                    "stage2": stage2,
                    "stage2Attempts": stage2_attempts,
                    "stage2Validation": stage2_validation,
                },
            )
        return OntologyRunResult(
            status="completed",
            stage="rdf",
            stage1_run_id=stage1_run_id,
            stage2_run_id=_optional_string(stage2.get("runId")),
            stage1_bundle=str(stage1_bundle),
            stage2_bundle=str(bundle),
            nquads_path=str(nquads),
            rdfxml_path=str(rdfxml),
            ntriples_path=str(ntriples),
            manifest_path=str(manifest),
            validation_path=str(validation),
            details={
                "stage1": stage1,
                "stage1Attempts": stage1_attempts,
                "stage1Mode": "template" if self.ontology_template else "generated",
                "stage1Validation": stage1_validation,
                "stage2": stage2,
                "stage2Attempts": stage2_attempts,
                "stage2Validation": stage2_validation,
            },
        )


def validate_ontology_result(result: OntologyRunResult) -> str | None:
    if result.status != "completed":
        return result.error_message or "Ontology pipeline did not complete."
    required = [
        result.stage1_bundle,
        result.nquads_path,
        result.rdfxml_path,
        result.ntriples_path,
        result.manifest_path,
        result.validation_path,
    ]
    if any(not value for value in required):
        return "Ontology pipeline did not return all required ontology and RDF artifact paths."
    stage1_bundle = Path(str(result.stage1_bundle))
    for name in ("ontology.json", "grounding.json"):
        path = stage1_bundle / name
        if not path.is_file() or path.stat().st_size == 0:
            return f"Reviewed Stage 1 artifact is missing or empty: {path}"
    return _rdf_invalid_reason(*(Path(str(value)) for value in required[1:]))


def _is_contract_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "ContractError"


def _config_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or path.is_file():
        return path
    packaged = Path(__file__).resolve().parents[4] / path
    return packaged if packaged.is_file() else path


def _optional_string(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _with_model_overrides(
    config: object,
    *,
    fields: tuple[str, ...],
    name: str | None,
    max_tokens: int | None,
    model_updates: dict[str, object],
) -> object:
    replacements = {}
    for field in fields:
        updates = dict(model_updates)
        if name:
            updates["name"] = name
        if max_tokens is not None:
            updates["max_tokens"] = max_tokens
        replacements[field] = replace(getattr(config, field), **updates)
    return replace(config, **replacements)


def _rdf_invalid_reason(
    nquads: Path,
    rdfxml: Path,
    ntriples: Path,
    manifest: Path,
    validation: Path,
) -> str | None:
    for label, path in (
        ("N-Quads", nquads),
        ("RDF/XML", rdfxml),
        ("N-Triples", ntriples),
        ("Stage 2 manifest", manifest),
        ("Stage 2 validation", validation),
    ):
        if not path.is_file() or path.stat().st_size == 0:
            return f"{label} artifact is missing or empty: {path}"
    try:
        validation_payload = json.loads(validation.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"Stage 2 validation is unreadable: {exc}"
    if not isinstance(validation_payload, dict) or validation_payload.get("status") != "valid":
        return "Stage 2 validation status is not valid."
    try:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"Stage 2 manifest is unreadable: {exc}"
    if not isinstance(manifest_payload, dict) or manifest_payload.get("status") not in {"complete", "completed"}:
        return "Stage 2 manifest status is not complete."
    return None


__all__ = ["AIIndexOntologyRunner", "validate_ontology_result"]
