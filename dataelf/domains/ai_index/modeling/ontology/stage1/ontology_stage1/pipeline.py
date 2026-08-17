from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, atomic_write_text, file_sha256, read_json_object, sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import (
    RunLock,
    append_event,
    candidate_root,
    configure_artifact_subdir,
    compatibility_fingerprints,
    load_checkpoint,
    make_checkpoint,
    new_run_id,
    published_root,
    save_checkpoint,
    select_resume,
    stage1_root,
    transition,
    utc_now,
)
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.domain_adapter import Stage1DomainAdapter
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.model_runtime import (
    ModelRuntimeError,
    ModelRuntimeTimeout,
    _normalize_singleton_evidence_arrays,
    run_generator,
    run_reviewer,
)
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.source import prepare_source_cache


Generator = Callable[..., tuple[dict[str, Any], dict[str, Any]]]
Reviewer = Callable[..., tuple[dict[str, Any], dict[str, Any]]]


def _round_root(workspace: Path, run_id: str, round_number: int) -> Path:
    return candidate_root(workspace, run_id) / f"round_{round_number:02d}"


def _save_round_artifact(workspace: Path, run_id: str, round_number: int, name: str, value: dict[str, Any]) -> Path:
    path = _round_root(workspace, run_id, round_number) / f"{name}.json"
    atomic_write_json(path, value)
    return path


def _load_candidate(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    return {"ontology": read_json_object(root / "ontology.json"), "grounding": read_json_object(root / "grounding.json")}


def _candidate_from_run(workspace: Path, run_id: str) -> tuple[dict[str, Any], Path]:
    checkpoint = load_checkpoint(workspace, run_id)
    candidate_path = checkpoint.get("currentCandidate")
    base_path: Path | None = None
    if isinstance(candidate_path, str) and Path(candidate_path).is_dir():
        base_path = Path(candidate_path)
    elif isinstance(checkpoint.get("repairBaseline"), str) and Path(str(checkpoint["repairBaseline"])).is_dir():
        base_path = Path(str(checkpoint["repairBaseline"]))
    if base_path is not None:
        candidate = _load_candidate(base_path)
        # A committed round is a safer repair baseline than a newer, interrupted
        # model turn.  The latter can contain intentionally sparse merge patches
        # that are not a complete replacement for the committed sections.
        if isinstance(candidate_path, str) and Path(candidate_path).is_dir():
            return candidate, base_path
        staged_files = sorted((candidate_root(workspace, run_id) / "runtime").glob("generator_round_*_staged.json"), reverse=True)
        if staged_files:
            staged = read_json_object(staged_files[0])
            sections = staged.get("sections", {})
            completed = set(staged.get("completedSections", []))
            if isinstance(sections, dict):
                for name in ("metadata", "classes", "objectProperties", "datatypeProperties"):
                    if name in sections and name in completed:
                        candidate["ontology"][name] = sections[name]
                for name in (
                    "tableClassifications", "columnClassifications", "classEvidence",
                    "objectPropertyEvidence", "datatypePropertyEvidence", "entityObservationMappings",
                    "accessPaths", "domainHintResolutions", "competencyQuestions", "cqCoverage", "sourceCoverage",
                    "sourceBindings", "sourceAccessPaths", "rawPathClassifications", "associationMappings",
                    "entityResolutionMappings", "responseObservationMappings", "relationAuthority",
                    "observationValueMappings", "relationSnapshotMappings", "iriGenerationMappings",
                    "shaclContract", "normalizationEvidenceRefs",
                ):
                    if name in sections and name in completed:
                        candidate["grounding"][name] = sections[name]
            return candidate, base_path
        return candidate, base_path
    published = published_root(workspace, run_id)
    if published.is_dir():
        return _load_candidate(published), published
    rounds = sorted(candidate_root(workspace, run_id).glob("round_*"), reverse=True)
    for root in rounds:
        if (root / "ontology.json").is_file() and (root / "grounding.json").is_file():
            return _load_candidate(root), root
    staged_files = sorted(
        (candidate_root(workspace, run_id) / "runtime").glob("generator_round_*_staged.json"),
        reverse=True,
    )
    for staged_path in staged_files:
        staged = read_json_object(staged_path)
        sections = staged.get("sections")
        if not isinstance(sections, dict) or not all(
            isinstance(sections.get(name), dict)
            for name in ("metadata", "classes", "objectProperties", "datatypeProperties")
        ):
            continue
        ontology = {
            "schemaVersion": "dataelf-ontology.v2",
            **{name: deepcopy(sections[name]) for name in ("metadata", "classes", "objectProperties", "datatypeProperties")},
        }
        grounding = {
            "schemaVersion": "dataelf-grounding.v2",
            "sourceFingerprint": checkpoint.get("compatibility", {}).get("source"),
        }
        for name in (
            "tableClassifications", "columnClassifications", "classEvidence", "objectPropertyEvidence",
            "datatypePropertyEvidence", "entityObservationMappings", "accessPaths", "domainHintResolutions",
            "competencyQuestions", "cqCoverage", "sourceCoverage", "sourceBindings", "sourceAccessPaths",
            "rawPathClassifications", "associationMappings", "entityResolutionMappings",
            "responseObservationMappings", "relationAuthority", "observationValueMappings",
            "relationSnapshotMappings", "iriGenerationMappings", "shaclContract", "normalizationEvidenceRefs",
        ):
            if name in sections:
                grounding[name] = deepcopy(sections[name])
        return {"ontology": ontology, "grounding": grounding}, staged_path
    raise ValueError(f"run {run_id} has no reusable ontology candidate")


def _prior_diagnostics(workspace: Path, run_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    visited: set[str] = set()
    current = run_id
    while current not in visited:
        visited.add(current)
        state = load_checkpoint(workspace, current)
        validation = None
        review = None
        if isinstance(state.get("currentValidation"), str) and Path(str(state["currentValidation"])).is_file():
            validation = read_json_object(str(state["currentValidation"]))
        if isinstance(state.get("currentReview"), str) and Path(str(state["currentReview"])).is_file():
            review = read_json_object(str(state["currentReview"]))
        audit_path = state.get("codexAudit")
        if isinstance(audit_path, str) and Path(audit_path).is_file():
            audit = read_json_object(audit_path)
            if audit.get("decision") == "revise" and isinstance(audit.get("findings"), list):
                merged_review = deepcopy(review) if review is not None else {
                    "schemaVersion": "dataelf-ontology-review.v2",
                    "summary": "Codex manual audit requested repair.",
                    "checkedEvidenceRefs": [],
                    "issues": [],
                }
                merged_review["verdict"] = "revise"
                merged_review.setdefault("issues", []).extend(
                    item for item in audit["findings"] if isinstance(item, dict)
                )
                review = merged_review
        if validation is not None or review is not None:
            return validation, review
        parent = state.get("repairFrom")
        if not isinstance(parent, str):
            break
        current = parent
    return None, None


def _write_candidate(workspace: Path, run_id: str, round_number: int, candidate: dict[str, Any]) -> Path:
    if not isinstance(candidate.get("ontology"), dict) or not isinstance(candidate.get("grounding"), dict):
        raise ValueError("candidate must contain ontology and grounding objects")
    root = _round_root(workspace, run_id, round_number)
    atomic_write_json(root / "ontology.json", candidate["ontology"])
    atomic_write_json(root / "grounding.json", candidate["grounding"])
    return root


def _publish(
    *,
    config: Stage1Config,
    workspace: Path,
    state: dict[str, Any],
    evidence: dict[str, Any],
    audit: dict[str, Any] | None,
    domain_adapter: Stage1DomainAdapter,
) -> Path:
    run_id = str(state["runId"])
    candidate_path = Path(str(state["currentCandidate"]))
    validation_path = Path(str(state["currentValidation"]))
    review_path = Path(str(state["currentReview"]))
    target = published_root(workspace, run_id)
    if target.exists():
        raise FileExistsError(f"published bundle already exists: {target}")
    temporary = target.parent / f".{run_id}.{os.getpid()}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for name, source in (
            ("ontology.json", candidate_path / "ontology.json"),
            ("grounding.json", candidate_path / "grounding.json"),
            ("validation.json", validation_path),
            ("review.json", review_path),
        ):
            shutil.copyfile(source, temporary / name)
        atomic_write_json(temporary / "evidence.json", evidence)
        source_index = evidence.get("sourceIndex")
        lineage = evidence.get("normalizationLineage")
        if evidence.get("sourceType") == "ai_index_raw" and (not isinstance(source_index, dict) or not isinstance(lineage, dict)):
            raise ValueError("v2 raw publication requires source_index and normalization_lineage")
        if isinstance(source_index, dict):
            atomic_write_json(temporary / "source_index.json", source_index)
        if isinstance(lineage, dict):
            atomic_write_json(temporary / "normalization_lineage.json", lineage)
        ontology = read_json_object(temporary / "ontology.json")
        grounding = read_json_object(temporary / "grounding.json")
        shacl_contract = grounding.get("shaclContract")
        if evidence.get("sourceType") == "ai_index_raw":
            if not isinstance(shacl_contract, dict) or shacl_contract.get("artifact") != "shacl.ttl":
                raise ValueError("v2 raw publication requires the deterministic SHACL contract")
            atomic_write_text(temporary / "shacl.ttl", domain_adapter.build_shacl_ttl(ontology))
        if audit is not None:
            atomic_write_json(temporary / "codex_audit.json", audit)
        artifact_names = ["ontology.json", "grounding.json", "evidence.json", "validation.json", "review.json"]
        if isinstance(source_index, dict):
            artifact_names.append("source_index.json")
        if isinstance(lineage, dict):
            artifact_names.append("normalization_lineage.json")
        if (temporary / "shacl.ttl").is_file():
            artifact_names.append("shacl.ttl")
        if audit is not None:
            artifact_names.append("codex_audit.json")
        manifest = {
            "schemaVersion": "dataelf-ontology-manifest.v2",
            "runId": run_id,
            "createdAt": utc_now(),
            "sourceFingerprint": evidence["sourceFingerprint"],
            "ontologyId": config.ontology.ontology_id,
            "artifacts": {
                name: {"sha256": file_sha256(temporary / name), "sizeBytes": (temporary / name).stat().st_size}
                for name in artifact_names
            },
            "compatibility": state["compatibility"],
            "modelRuntime": state.get("modelRuntime", {}),
            "manualAuditRequired": config.quality.manual_audit_required,
            "rawSources": [
                {
                    "relativeFile": item.get("relativeFile"),
                    "sha256": item.get("sha256"),
                    "sizeBytes": item.get("sizeBytes"),
                    "endpoint": item.get("endpoint"),
                }
                for item in (source_index.get("documents", []) if isinstance(source_index, dict) else [])
            ],
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    latest = {
        "schemaVersion": "dataelf-ontology-latest.v2",
        "runId": run_id,
        "publishedAt": utc_now(),
        "bundle": str(target),
        "ontologySha256": file_sha256(target / "ontology.json"),
        "manifestSha256": file_sha256(target / "manifest.json"),
    }
    atomic_write_json(stage1_root(workspace) / "latest.json", latest)
    transition(workspace, state, "completed", publishedBundle=str(target))
    append_event(workspace, run_id, "published", bundle=str(target), artifactCount=len(manifest["artifacts"]))
    return target


def generate_pipeline(
    *,
    config: Stage1Config,
    workspace: Path,
    resume: str | None = "auto",
    repair_from: str | None = None,
    domain_adapter: Stage1DomainAdapter,
    generator: Generator = run_generator,
    reviewer: Reviewer = run_reviewer,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    configure_artifact_subdir(config)
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")
    cache_root, evidence, cache_reused = prepare_source_cache(workspace, config)
    evidence_path = cache_root / "evidence.json"
    compatibility = compatibility_fingerprints(
        config, evidence["sourceFingerprint"], domain_adapter.prompt_fingerprint()
    )
    if repair_from and resume not in {None, "auto"}:
        raise ValueError("--repair-from cannot be combined with an explicit --resume run id")
    state = None if repair_from else select_resume(workspace, resume, compatibility)
    baseline: dict[str, Any] | None = None
    feedback: dict[str, Any] | None = None
    imported_validation: dict[str, Any] | None = None
    imported_normalization_count = 0
    if state is None:
        run_id = new_run_id()
        state = make_checkpoint(run_id, compatibility, cache_root)
        if repair_from:
            baseline, baseline_path = _candidate_from_run(workspace, repair_from)
            if baseline["grounding"].get("sourceFingerprint") != evidence["sourceFingerprint"]:
                raise ValueError("repair source fingerprint differs from the current raw-backed source")
            baseline, imported_normalization_count = _normalize_singleton_evidence_arrays(baseline)
            baseline, imported_contract_normalization = domain_adapter.normalize_candidate_contract(
                baseline, evidence, config
            )
            imported_validation = domain_adapter.validate_candidate(
                baseline["ontology"], baseline["grounding"], evidence, config
            )
            imported_seed = candidate_root(workspace, run_id) / "imported_seed"
            atomic_write_json(imported_seed / "ontology.json", baseline["ontology"])
            atomic_write_json(imported_seed / "grounding.json", baseline["grounding"])
            prior_validation, prior_review = _prior_diagnostics(workspace, repair_from)
            feedback = domain_adapter.repair_feedback(
                imported_validation if imported_validation.get("status") != "valid" else prior_validation,
                prior_review,
            )
            feedback.update({
                "instruction": "Revalidate and repair this imported candidate under the current implementation and prompts.",
                "importedFromRun": repair_from,
                "stagedSectionsImported": baseline_path != Path(str(load_checkpoint(workspace, repair_from).get("currentCandidate", ""))),
                "controllerContractNormalization": imported_contract_normalization,
            })
            state["repairFrom"] = repair_from
            state["repairBaseline"] = str(imported_seed)
            state["feedback"] = feedback
        save_checkpoint(workspace, state)
        append_event(workspace, run_id, "run_created", cacheReused=cache_reused, repairFrom=repair_from)
        if repair_from and baseline is not None and imported_validation is not None and imported_validation.get("status") == "valid":
            candidate_path = _write_candidate(workspace, run_id, 0, baseline)
            validation_path = _save_round_artifact(workspace, run_id, 0, "validation", imported_validation)
            state.setdefault("modelRuntime", {})["controllerImportedRepair"] = {
                "modelCalls": 0,
                "singletonEvidenceNormalizations": imported_normalization_count,
                "contractNormalization": imported_contract_normalization,
                "importedFromRun": repair_from,
            }
            transition(workspace, state, "validated", currentCandidate=str(candidate_path), currentValidation=str(validation_path), round=0)
            append_event(
                workspace,
                run_id,
                "imported_candidate_deterministically_repaired",
                importedFromRun=repair_from,
                singletonEvidenceNormalizations=imported_normalization_count,
                status="valid",
            )
    else:
        run_id = str(state["runId"])
        append_event(workspace, run_id, "run_resumed", stage=state.get("stage"), round=state.get("round"), cacheReused=cache_reused)
        if state.get("stage") == "awaiting_manual_audit":
            return {"runId": run_id, "status": "awaiting_manual_audit", "checkpoint": str(state.get("updatedAt"))}
    with RunLock(workspace, run_id):
        while True:
            round_number = int(state.get("round", 0))
            if round_number > config.quality.max_repair_rounds:
                transition(workspace, state, "terminal_failed", reason="repair_budget_exhausted")
                append_event(workspace, run_id, "terminal_failed", reason="repair_budget_exhausted")
                return {"runId": run_id, "status": "terminal_failed", "reason": "repair_budget_exhausted"}
            stage = str(state.get("stage"))
            if stage in {"source_profiled", "repair_pending"} or (stage in {"paused_timeout", "paused_interrupted", "paused_runtime_error"} and state.get("pausedComponent") == "generator"):
                if stage == "repair_pending":
                    feedback = state.get("feedback") if isinstance(state.get("feedback"), dict) else None
                    baseline_path = state.get("currentCandidate")
                    baseline = _load_candidate(str(baseline_path)) if isinstance(baseline_path, str) else baseline
                resume_runtime = stage in {"paused_timeout", "paused_interrupted", "paused_runtime_error"} or bool(state.get("generatorStartedForRound") == round_number)
                transition(workspace, state, "generator_running", round=round_number, generatorStartedForRound=round_number)
                try:
                    candidate, generator_metadata = generator(
                        config=config,
                        workspace=workspace,
                        run_id=run_id,
                        round_number=round_number,
                        evidence=evidence,
                        evidence_path=evidence_path,
                        feedback=feedback,
                        baseline=baseline,
                        resume_runtime=resume_runtime,
                        domain_adapter=domain_adapter,
                    )
                except ModelRuntimeTimeout:
                    transition(workspace, state, "paused_timeout", pausedComponent="generator", round=round_number)
                    append_event(workspace, run_id, "paused_timeout", component="generator", round=round_number)
                    return {"runId": run_id, "status": "paused_timeout", "component": "generator"}
                except ModelRuntimeError as exc:
                    transition(workspace, state, "paused_runtime_error", pausedComponent="generator", round=round_number, runtimeError=str(exc)[:2000])
                    append_event(workspace, run_id, "paused_runtime_error", component="generator", round=round_number, error=str(exc)[:2000])
                    return {"runId": run_id, "status": "paused_runtime_error", "component": "generator", "error": str(exc)}
                except KeyboardInterrupt:
                    transition(workspace, state, "paused_interrupted", pausedComponent="generator", round=round_number)
                    append_event(workspace, run_id, "paused_interrupted", component="generator", round=round_number)
                    raise
                candidate_path = _write_candidate(workspace, run_id, round_number, candidate)
                state.setdefault("modelRuntime", {})[f"generatorRound{round_number}"] = generator_metadata
                transition(workspace, state, "candidate_staged", currentCandidate=str(candidate_path), round=round_number)
                append_event(workspace, run_id, "candidate_staged", round=round_number, path=str(candidate_path))
                stage = "candidate_staged"
            if stage == "generator_running":
                # The process died without a controlled timeout. Its staged sections are reusable.
                transition(workspace, state, "paused_timeout", pausedComponent="generator", round=round_number)
                continue
            if stage == "candidate_staged":
                candidate = _load_candidate(str(state["currentCandidate"]))
                validation = domain_adapter.validate_candidate(
                    candidate["ontology"], candidate["grounding"], evidence, config
                )
                validation_path = _save_round_artifact(workspace, run_id, round_number, "validation", validation)
                transition(workspace, state, "validated", currentValidation=str(validation_path), round=round_number)
                append_event(workspace, run_id, "deterministic_validation", round=round_number, status=validation["status"], errors=len(validation["errors"]))
                stage = "validated"
            if stage == "validated":
                validation = read_json_object(str(state["currentValidation"]))
                if validation.get("status") != "valid":
                    if round_number >= config.quality.max_repair_rounds:
                        transition(workspace, state, "terminal_failed", reason="deterministic_validation_repair_budget_exhausted")
                        return {"runId": run_id, "status": "terminal_failed", "reason": "deterministic_validation_repair_budget_exhausted"}
                    feedback = domain_adapter.repair_feedback(validation, None)
                    transition(workspace, state, "repair_pending", round=round_number + 1, feedback=feedback)
                    append_event(workspace, run_id, "repair_requested", source="validator", nextRound=round_number + 1, issueCount=len(validation.get("errors", [])))
                    continue
                transition(workspace, state, "reviewer_running", round=round_number)
                stage = "reviewer_running"
            if stage in {"paused_timeout", "paused_interrupted", "paused_runtime_error"} and state.get("pausedComponent") == "reviewer":
                transition(workspace, state, "reviewer_running", round=round_number)
                stage = "reviewer_running"
            if stage == "reviewer_running":
                candidate = _load_candidate(str(state["currentCandidate"]))
                validation = read_json_object(str(state["currentValidation"]))
                try:
                    review, reviewer_metadata = reviewer(
                        config=config,
                        workspace=workspace,
                        run_id=run_id,
                        round_number=round_number,
                        evidence=evidence,
                        evidence_path=evidence_path,
                        candidate=candidate,
                        validation=validation,
                        domain_adapter=domain_adapter,
                    )
                except ModelRuntimeTimeout:
                    transition(workspace, state, "paused_timeout", pausedComponent="reviewer", round=round_number)
                    append_event(workspace, run_id, "paused_timeout", component="reviewer", round=round_number)
                    return {"runId": run_id, "status": "paused_timeout", "component": "reviewer"}
                except ModelRuntimeError as exc:
                    transition(workspace, state, "paused_runtime_error", pausedComponent="reviewer", round=round_number, runtimeError=str(exc)[:2000])
                    append_event(workspace, run_id, "paused_runtime_error", component="reviewer", round=round_number, error=str(exc)[:2000])
                    return {"runId": run_id, "status": "paused_runtime_error", "component": "reviewer", "error": str(exc)}
                except KeyboardInterrupt:
                    transition(workspace, state, "paused_interrupted", pausedComponent="reviewer", round=round_number)
                    append_event(workspace, run_id, "paused_interrupted", component="reviewer", round=round_number)
                    raise
                review_errors = domain_adapter.validate_review(review, evidence, config)
                if review_errors:
                    failed_check = {"status": "fail", "summary": "Reviewer response did not satisfy the controller contract.", "evidenceRefs": []}
                    review = {
                        "schemaVersion": "dataelf-ontology-review.v2",
                        "verdict": "revise",
                        "summary": "Reviewer response failed its controller-owned contract.",
                        "issues": [
                            {
                                "severity": "high",
                                "category": "review_contract",
                                "path": "/review",
                                "message": message,
                                "evidenceRefs": [],
                                "requiredChange": "Return a review that satisfies the review schema and evidence-reference contract.",
                                "acceptanceCriteria": "validate_review reports no errors.",
                            }
                            for message in review_errors
                        ],
                        "checkedEvidenceRefs": [],
                        "checks": {
                            name: dict(failed_check)
                            for name in (
                                "informationCompleteness", "sourceNavigability", "missingnessSemantics",
                                "associationEndpoints", "observationMetrics", "multivalueConcepts",
                                "relationAuthority", "competencyQuestionExecutability",
                                "instanceIdentity", "constraintExecutability",
                            )
                        },
                    }
                review_path = _save_round_artifact(workspace, run_id, round_number, "review", review)
                state.setdefault("modelRuntime", {})[f"reviewerRound{round_number}"] = reviewer_metadata
                transition(workspace, state, "reviewed", currentReview=str(review_path), round=round_number)
                append_event(workspace, run_id, "review_complete", round=round_number, verdict=review.get("verdict"), issueCount=len(review.get("issues", [])))
                stage = "reviewed"
            if stage == "reviewed":
                review = read_json_object(str(state["currentReview"]))
                verdict = review.get("verdict")
                if verdict == "unusable":
                    transition(workspace, state, "terminal_failed", reason="reviewer_unusable")
                    return {"runId": run_id, "status": "terminal_failed", "reason": "reviewer_unusable"}
                if verdict == "revise":
                    if round_number >= config.quality.max_repair_rounds:
                        transition(workspace, state, "terminal_failed", reason="reviewer_repair_budget_exhausted")
                        return {"runId": run_id, "status": "terminal_failed", "reason": "reviewer_repair_budget_exhausted"}
                    feedback = domain_adapter.repair_feedback(None, review)
                    transition(workspace, state, "repair_pending", round=round_number + 1, feedback=feedback)
                    append_event(workspace, run_id, "repair_requested", source="reviewer", nextRound=round_number + 1, issueCount=len(review.get("issues", [])))
                    continue
                if verdict != "approve":
                    raise ValueError(f"unsupported reviewer verdict: {verdict}")
                if config.quality.manual_audit_required:
                    transition(workspace, state, "awaiting_manual_audit", round=round_number)
                    append_event(workspace, run_id, "awaiting_manual_audit", ontologySha256=file_sha256(Path(str(state["currentCandidate"])) / "ontology.json"))
                    return {"runId": run_id, "status": "awaiting_manual_audit", "candidate": state["currentCandidate"]}
                bundle = _publish(
                    config=config,
                    workspace=workspace,
                    state=state,
                    evidence=evidence,
                    audit=None,
                    domain_adapter=domain_adapter,
                )
                return {"runId": run_id, "status": "completed", "bundle": str(bundle)}
            if stage == "awaiting_manual_audit":
                return {"runId": run_id, "status": "awaiting_manual_audit", "candidate": state["currentCandidate"]}
            if stage in {"completed", "terminal_failed"}:
                return {"runId": run_id, "status": stage, "bundle": state.get("publishedBundle"), "reason": state.get("reason")}
            raise RuntimeError(f"cannot continue run {run_id} from stage {stage}")


def apply_manual_audit(
    *,
    config: Stage1Config,
    workspace: Path,
    run_id: str,
    report: dict[str, Any],
    domain_adapter: Stage1DomainAdapter,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    configure_artifact_subdir(config)
    state = load_checkpoint(workspace, run_id)
    if state.get("stage") != "awaiting_manual_audit":
        raise ValueError(f"run {run_id} is not awaiting manual audit")
    decision = report.get("decision")
    if decision not in {"approve", "revise"}:
        raise ValueError("audit decision must be approve or revise")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise ValueError("audit summary is required")
    findings = report.get("findings", [])
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise ValueError("audit findings must be an object array")
    blocking = [item for item in findings if item.get("severity") in config.quality.blocking_severities]
    if decision == "approve" and blocking:
        raise ValueError("manual approval cannot contain blocking-severity findings")
    candidate_path = Path(str(state["currentCandidate"]))
    evidence = read_json_object(Path(str(state["sourceCache"])) / "evidence.json")
    audit = {
        "schemaVersion": "dataelf-codex-audit.v1",
        "auditor": "Codex",
        "auditedAt": utc_now(),
        "decision": decision,
        "summary": report["summary"],
        "findings": findings,
        "bindings": {
            "ontologySha256": file_sha256(candidate_path / "ontology.json"),
            "groundingSha256": file_sha256(candidate_path / "grounding.json"),
            "evidenceSha256": sha256_json(evidence),
            "validationSha256": file_sha256(str(state["currentValidation"])),
            "reviewSha256": file_sha256(str(state["currentReview"])),
            "sourceIndexSha256": (evidence.get("sourceIndex") or {}).get("sourceIndexSha256"),
            "normalizationLineageSha256": (evidence.get("normalizationLineage") or {}).get("lineageSha256"),
        },
    }
    atomic_write_json(candidate_root(workspace, run_id) / "codex_audit.json", audit)
    if decision == "revise":
        round_number = int(state.get("round", 0))
        if round_number >= config.quality.max_repair_rounds:
            transition(workspace, state, "terminal_failed", reason="manual_audit_repair_budget_exhausted", codexAudit=str(candidate_root(workspace, run_id) / "codex_audit.json"))
            return {"runId": run_id, "status": "terminal_failed", "reason": "manual_audit_repair_budget_exhausted"}
        feedback = domain_adapter.repair_feedback(
            None,
            {
                "schemaVersion": "dataelf-ontology-review.v2",
                "verdict": "revise",
                "summary": report["summary"],
                "issues": findings,
            },
        )
        feedback["source"] = "codex_manual_audit"
        transition(
            workspace,
            state,
            "repair_pending",
            round=round_number + 1,
            feedback=feedback,
            codexAudit=str(candidate_root(workspace, run_id) / "codex_audit.json"),
        )
        append_event(workspace, run_id, "manual_audit_revise", findingCount=len(findings))
        return {"runId": run_id, "status": "repair_pending", "nextRound": round_number + 1}
    with RunLock(workspace, run_id):
        bundle = _publish(
            config=config,
            workspace=workspace,
            state=state,
            evidence=evidence,
            audit=audit,
            domain_adapter=domain_adapter,
        )
    return {"runId": run_id, "status": "completed", "bundle": str(bundle)}


def validate_published_bundle(
    bundle: Path,
    config: Stage1Config,
    workspace: Path | None = None,
    domain_adapter: Stage1DomainAdapter | None = None,
) -> dict[str, Any]:
    if domain_adapter is None:
        raise ValueError("validate_published_bundle requires an explicit Stage 1 domain adapter")
    bundle = bundle.resolve()
    manifest = read_json_object(bundle / "manifest.json")
    hash_errors: list[str] = []
    for name, metadata in manifest.get("artifacts", {}).items():
        path = bundle / name
        if not path.is_file():
            hash_errors.append(f"missing artifact: {name}")
        elif not isinstance(metadata, dict) or file_sha256(path) != metadata.get("sha256"):
            hash_errors.append(f"artifact hash mismatch: {name}")
    ontology = read_json_object(bundle / "ontology.json")
    grounding = read_json_object(bundle / "grounding.json")
    evidence = read_json_object(bundle / "evidence.json")
    validation = domain_adapter.validate_candidate(ontology, grounding, evidence, config)
    review = read_json_object(bundle / "review.json")
    review_errors = domain_adapter.validate_review(review, evidence, config)
    source_replay_errors: list[str] = []
    source_index_path = bundle / "source_index.json"
    if source_index_path.is_file():
        from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.raw_semantics import replay_source_index

        if workspace is None:
            root = bundle.parent.parent
            subdir_parts = Path(config.artifacts.subdir).parts
            if not subdir_parts or root.parts[-len(subdir_parts):] != subdir_parts:
                raise ValueError(f"bundle is not under configured Stage 1 artifact root: {bundle}")
            workspace = Path(*root.parts[:-len(subdir_parts)])
        source_replay_errors = replay_source_index(workspace.resolve(), read_json_object(source_index_path))
    elif evidence.get("sourceType") == "ai_index_raw":
        source_replay_errors = ["missing artifact: source_index.json"]
    shacl_errors: list[str] = []
    shacl_path = bundle / "shacl.ttl"
    if evidence.get("sourceType") == "ai_index_raw":
        if not shacl_path.is_file():
            shacl_errors = ["missing artifact: shacl.ttl"]
        else:
            try:
                shacl_errors = domain_adapter.shacl_contract_errors(
                    ontology, shacl_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                shacl_errors = [f"invalid shacl.ttl: {exc}"]
    valid = (
        not hash_errors and not source_replay_errors and not shacl_errors and validation["status"] == "valid"
        and not review_errors and review.get("verdict") == "approve"
    )
    return {
        "schemaVersion": "dataelf-bundle-validation.v1",
        "status": "valid" if valid else "invalid",
        "bundle": str(bundle),
        "hashErrors": hash_errors,
        "candidateValidation": validation,
        "reviewErrors": review_errors,
        "reviewVerdict": review.get("verdict"),
        "sourceReplayErrors": source_replay_errors,
        "shaclErrors": shacl_errors,
    }
