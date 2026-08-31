from __future__ import annotations

import dataclasses
import fcntl
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import (
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    read_json_object,
    sha256_json,
)
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import utc_now
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.compiler import (
    ENDPOINT_SLUGS,
    ModelRunner,
    compile_plan,
    load_plans,
    stage2_root,
)
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import Stage1Contract, resolve_stage1_contract
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.model_runtime import ensure_stage_time_remaining, stage_deadline
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.rdf import MaterializedGraph, materialize, nquads, ntriples, rdfxml
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.reviewer import (
    ReviewRunner,
    build_review_context,
    run_review,
)
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.validation import validate_candidate, validate_serialization_files


PIPELINE_VERSION = "dataelf-stage2-pipeline.v2"
TERMINAL_STAGES = {"completed", "candidate_approved", "terminal_failed", "manual_revise"}


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    return value


def implementation_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    return sha256_json(
        {
            path.relative_to(root).as_posix(): file_sha256(path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".ts", ".yaml", ".json"}
        }
    )


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)


def _state_path(workspace: Path, config: Stage2Config, run_id: str) -> Path:
    return stage2_root(workspace, config) / ".checkpoints" / "runs" / run_id / "pipeline.json"


def _candidate_root(workspace: Path, config: Stage2Config, run_id: str, round_number: int) -> Path:
    return stage2_root(workspace, config) / "candidates" / run_id / f"round_{round_number:02d}"


def _published_root(workspace: Path, config: Stage2Config, run_id: str) -> Path:
    return stage2_root(workspace, config) / "published" / run_id


class _RunLock:
    def __init__(self, workspace: Path, config: Stage2Config, run_id: str) -> None:
        self.path = _state_path(workspace, config, run_id).parent / "run.lock"
        self.handle: Any = None

    def __enter__(self) -> "_RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError(f"Stage 2 run is already locked: {self.path.parent.name}") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _save_state(workspace: Path, config: Stage2Config, state: dict[str, Any], stage: str | None = None, **updates: Any) -> None:
    if stage:
        state["stage"] = stage
        state.setdefault("history", []).append({"at": utc_now(), "stage": stage, "round": state.get("round")})
    state.update(updates)
    state["updatedAt"] = utc_now()
    atomic_write_json(_state_path(workspace, config, str(state["runId"])), state)


def _identity(config: Stage2Config, contract: Stage1Contract, plan_hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "pipelineVersion": PIPELINE_VERSION,
        "contract": contract.contract_fingerprint,
        "source": contract.source_fingerprint,
        "sourceIndex": contract.source_index["sourceIndexSha256"],
        "plans": dict(sorted(plan_hashes.items())),
        "config": sha256_json(_jsonable(config)),
        "implementation": implementation_fingerprint(),
    }


def _write_candidate_files(
    root: Path,
    graph: MaterializedGraph,
    plans: dict[str, dict[str, Any]],
    contract: Stage1Contract,
    config: Stage2Config,
) -> tuple[dict[str, Any], dict[str, str]]:
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / config.output.nquads_name, nquads(graph))
    atomic_write_text(root / config.output.ntriples_name, ntriples(graph))
    (root / config.output.rdfxml_name).parent.mkdir(parents=True, exist_ok=True)
    temporary_xml = root / f".{config.output.rdfxml_name}.tmp"
    temporary_xml.write_bytes(rdfxml(graph, str(contract.ontology["metadata"]["namespace"]), config.provenance_namespace))
    os.replace(temporary_xml, root / config.output.rdfxml_name)
    atomic_write_json(root / "extraction_plans.json", {"schemaVersion": "dataelf-stage2-plan-set.v2", "plans": plans})
    atomic_write_json(root / "projection_lineage.json", graph.projection_lineage)
    atomic_write_json(
        root / "unresolved_references.json",
        {"schemaVersion": "dataelf-stage2-unresolved-references.v2", "references": graph.unresolved_references},
    )
    atomic_write_json(
        root / "reference_only_entities.json",
        {
            "schemaVersion": "dataelf-stage2-reference-only-entities.v1",
            "entities": graph.reference_only_entities,
        },
    )
    atomic_write_json(root / "metrics.json", {"schemaVersion": "dataelf-stage2-metrics.v2", "metrics": graph.metrics, "diagnostics": graph.diagnostics})
    plan_hashes = {endpoint: plan["planSha256"] for endpoint, plan in plans.items()}
    validation = validate_candidate(
        graph=graph,
        contract=contract,
        config=config,
        nq_path=root / config.output.nquads_name,
        nt_path=root / config.output.ntriples_name,
        rdfxml_path=root / config.output.rdfxml_name,
        plan_hashes=plan_hashes,
    )
    atomic_write_json(root / config.output.validation_name, validation)
    context = build_review_context(graph=graph, validation=validation, plans=plans, contract=contract)
    atomic_write_json(root / "review_context.json", context)
    return validation, plan_hashes


def _artifact_record(path: Path) -> dict[str, Any]:
    return {"sha256": file_sha256(path), "sizeBytes": path.stat().st_size}


def _write_candidate_manifest(
    *,
    root: Path,
    config: Stage2Config,
    state: dict[str, Any],
    contract: Stage1Contract,
    status: str,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    names = [
        config.output.nquads_name,
        config.output.ntriples_name,
        config.output.rdfxml_name,
        "extraction_plans.json",
        "projection_lineage.json",
        "unresolved_references.json",
        "reference_only_entities.json",
        "metrics.json",
        config.output.validation_name,
    ]
    for optional in ("review.json", "codex_audit.json"):
        if (root / optional).is_file():
            names.append(optional)
    manifest = {
        "schemaVersion": "dataelf-stage2-candidate-manifest.v2",
        "status": status,
        "createdAt": utc_now(),
        "runId": state["runId"],
        "round": state["round"],
        "stage1RunId": contract.run_id,
        "stage1Draft": contract.is_draft,
        "stage1Bundle": str(contract.bundle),
        "contractFingerprint": contract.contract_fingerprint,
        "sourceFingerprint": contract.source_fingerprint,
        "sourceIndexSha256": contract.source_index["sourceIndexSha256"],
        "identity": state["identity"],
        "modelRuntime": runtime or state.get("reviewerRuntime"),
        "modelCallCount": state.get("modelCallCount", 0),
        "modelCalls": {
            "compiledPlans": state.get("compiledPlanModelCallCount", 0),
            "reviewerThisRun": state.get("reviewerModelCallCount", 0),
            "thisRunIncludingCompiler": state.get("modelCallCount", 0),
            "boundArtifactTotal": int(state.get("compiledPlanModelCallCount", 0))
            + int(state.get("reviewerModelCallCount", 0)),
        },
        "artifacts": {name: _artifact_record(root / name) for name in names},
        "latestUpdated": False,
        "stableRdfUpdated": False,
    }
    atomic_write_json(root / "candidate_manifest.json", manifest)
    return manifest


def _repair_feedback(review: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    issues = [issue for issue in review.get("issues", []) if isinstance(issue, dict)]
    endpoints = {endpoint for issue in issues for endpoint in issue.get("affectedEndpoints", [])}
    if not endpoints:
        endpoints = set(ENDPOINT_SLUGS)
    return {
        "verdict": review.get("verdict"),
        "issues": [
            {
                key: issue.get(key)
                for key in ("severity", "category", "path", "message", "evidenceRefs", "requiredChange", "acceptanceCriteria")
            }
            for issue in issues
        ],
    }, endpoints


def _copy_plan_set(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for slug in ENDPOINT_SLUGS.values():
        shutil.copyfile(source / f"{slug}.plan.json", target / f"{slug}.plan.json")


def _promote(
    *,
    workspace: Path,
    config: Stage2Config,
    state: dict[str, Any],
    contract: Stage1Contract,
    candidate: Path,
) -> Path:
    if contract.is_draft:
        raise RuntimeError("draft Stage 1 input can never promote a stable Stage 2 RDF")
    target = _published_root(workspace, config, str(state["runId"]))
    if target.exists():
        raise FileExistsError(f"published Stage 2 bundle already exists: {target}")
    temporary = target.parent / f".{state['runId']}.{os.getpid()}.tmp"
    shutil.copytree(candidate, temporary)
    candidate_manifest = read_json_object(temporary / "candidate_manifest.json")
    published_manifest = {
        **candidate_manifest,
        "schemaVersion": "dataelf-stage2-manifest.v2",
        "status": "complete",
        "publishedAt": utc_now(),
        "latestUpdated": True,
        "stableRdfUpdated": True,
    }
    atomic_write_json(temporary / config.output.manifest_name, published_manifest)
    (temporary / "candidate_manifest.json").unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)
    stable = workspace.parent / f"{workspace.name}{config.output.stable_suffix}"
    stable_tmp = stable.parent / f".{stable.name}.{os.getpid()}.tmp"
    shutil.copyfile(target / config.output.rdfxml_name, stable_tmp)
    os.replace(stable_tmp, stable)
    latest = {
        "schemaVersion": "dataelf-stage2-latest.v2",
        "runId": state["runId"],
        "publishedAt": utc_now(),
        "bundle": str(target),
        "stableRdf": str(stable),
        "rdfSha256": file_sha256(stable),
        "manifestSha256": file_sha256(target / config.output.manifest_name),
    }
    atomic_write_json(stage2_root(workspace, config) / "latest.json", latest)
    _save_state(workspace, config, state, "completed", publishedBundle=str(target), stableRdf=str(stable))
    return target


def build(
    config: Stage2Config,
    workspace: Path,
    *,
    resume_run_id: str | None = None,
    stage1_bundle: Path | None = None,
    allow_draft: bool = False,
    model_runner: ModelRunner | None = None,
    review_runner: ReviewRunner | None = None,
) -> dict[str, Any]:
    with stage_deadline(config.total_stage_timeout_seconds):
        return _build(
            config,
            workspace,
            resume_run_id=resume_run_id,
            stage1_bundle=stage1_bundle,
            allow_draft=allow_draft,
            model_runner=model_runner,
            review_runner=review_runner,
        )


def _build(
    config: Stage2Config,
    workspace: Path,
    *,
    resume_run_id: str | None = None,
    stage1_bundle: Path | None = None,
    allow_draft: bool = False,
    model_runner: ModelRunner | None = None,
    review_runner: ReviewRunner | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    ensure_stage_time_remaining()
    if resume_run_id:
        resume_state = read_json_object(_state_path(workspace, config, resume_run_id))
        if resume_state.get("stage1Draft"):
            stage1_bundle = Path(str(resume_state["stage1Bundle"]))
            allow_draft = True
    compile_result = compile_plan(
        config,
        workspace,
        stage1_bundle=stage1_bundle,
        allow_draft=allow_draft,
        model_runner=model_runner,
    )
    ensure_stage_time_remaining()
    plans, contract, plans_root = load_plans(
        config,
        workspace,
        stage1_bundle=stage1_bundle,
        allow_draft=allow_draft,
    )
    plan_hashes = {endpoint: plan["planSha256"] for endpoint, plan in plans.items()}
    identity = _identity(config, contract, plan_hashes)
    if resume_run_id:
        state = read_json_object(_state_path(workspace, config, resume_run_id))
        if state.get("stage") in TERMINAL_STAGES:
            raise ValueError("Stage 2 checkpoint is terminal")
        if state.get("identity", {}).get("implementation") != identity["implementation"] or state.get("identity", {}).get("contract") != identity["contract"]:
            raise ValueError("Stage 2 checkpoint is incompatible with current code or Stage 1 contract")
        run_id = resume_run_id
        plans_root = Path(str(state["currentPlansRoot"]))
        plans, contract, _ = load_plans(
            config,
            workspace,
            stage1_bundle=Path(str(state["stage1Bundle"])) if state.get("stage1Draft") else None,
            allow_draft=bool(state.get("stage1Draft")),
            root=plans_root,
        )
    else:
        run_id = _new_run_id()
        state = {
            "checkpointVersion": 2,
            "runId": run_id,
            "stage": "initialized",
            "round": 0,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "identity": identity,
            "stage1Bundle": str(contract.bundle),
            "stage1Draft": contract.is_draft,
            "currentPlansRoot": str(plans_root),
            "modelCallCount": int(compile_result.get("modelCallCount", 0)),
            "compiledPlanModelCallCount": int(compile_result.get("compiledModelCallCount", 0)),
            "reviewerModelCallCount": 0,
            "history": [{"at": utc_now(), "stage": "initialized", "round": 0}],
        }
        atomic_write_json(_state_path(workspace, config, run_id), state)

    with _RunLock(workspace, config, run_id):
        while True:
            round_number = int(state["round"])
            candidate = _candidate_root(workspace, config, run_id, round_number)
            stage = str(state["stage"])
            if stage == "initialized":
                ensure_stage_time_remaining()
                graph = materialize(plans, contract, config)
                validation, current_hashes = _write_candidate_files(candidate, graph, plans, contract, config)
                state["identity"]["plans"] = dict(sorted(current_hashes.items()))
                if validation["status"] != "valid":
                    _write_candidate_manifest(root=candidate, config=config, state=state, contract=contract, status="deterministic_invalid")
                    _save_state(workspace, config, state, "terminal_failed", reason="deterministic_validation_failed", candidateBundle=str(candidate))
                    return {"runId": run_id, "status": "terminal_failed", "bundle": str(candidate), "validation": validation}
                _save_state(workspace, config, state, "review_pending", candidateBundle=str(candidate))
                stage = "review_pending"
            if stage == "review_pending":
                ensure_stage_time_remaining()
                context = read_json_object(candidate / "review_context.json")
                review, reviewer_runtime = run_review(
                    config=config,
                    context=context,
                    candidate_root=candidate,
                    runner=review_runner,
                )
                atomic_write_json(candidate / "review.json", review)
                state["modelCallCount"] = int(state.get("modelCallCount", 0)) + 1
                state["reviewerModelCallCount"] = int(state.get("reviewerModelCallCount", 0)) + 1
                _save_state(workspace, config, state, "reviewed", reviewerRuntime=reviewer_runtime)
                stage = "reviewed"
            if stage == "reviewed":
                review = read_json_object(candidate / "review.json")
                verdict = str(review["verdict"])
                if verdict == "unusable":
                    _write_candidate_manifest(root=candidate, config=config, state=state, contract=contract, status="reviewer_unusable")
                    _save_state(workspace, config, state, "terminal_failed", reason="reviewer_unusable")
                    return {"runId": run_id, "status": "terminal_failed", "bundle": str(candidate), "review": review}
                if verdict == "revise":
                    if round_number + 1 >= config.quality.max_repair_rounds:
                        _write_candidate_manifest(root=candidate, config=config, state=state, contract=contract, status="repair_rounds_exhausted")
                        _save_state(workspace, config, state, "terminal_failed", reason="repair_rounds_exhausted")
                        return {"runId": run_id, "status": "terminal_failed", "bundle": str(candidate), "review": review}
                    feedback, affected = _repair_feedback(review)
                    next_root = stage2_root(workspace, config) / ".checkpoints" / "runs" / run_id / "plans" / f"round_{round_number + 1:02d}"
                    _copy_plan_set(plans_root, next_root)
                    repaired = compile_plan(
                        config,
                        workspace,
                        replace=True,
                        stage1_bundle=contract.bundle if contract.is_draft else None,
                        allow_draft=contract.is_draft,
                        model_runner=model_runner,
                        feedback=feedback,
                        affected_endpoints=affected,
                        output_root=next_root,
                    )
                    state["modelCallCount"] = int(state.get("modelCallCount", 0)) + int(repaired.get("modelCallCount", 0))
                    state["compiledPlanModelCallCount"] = int(state.get("compiledPlanModelCallCount", 0)) + int(
                        repaired.get("modelCallCount", 0)
                    )
                    state["round"] = round_number + 1
                    state["currentPlansRoot"] = str(next_root)
                    plans_root = next_root
                    plans, contract, _ = load_plans(
                        config,
                        workspace,
                        stage1_bundle=contract.bundle if contract.is_draft else None,
                        allow_draft=contract.is_draft,
                        root=plans_root,
                    )
                    _save_state(workspace, config, state, "initialized", repairFromRound=round_number)
                    continue
                audit = {
                    "schemaVersion": "dataelf-stage2-codex-audit.v2",
                    "decision": "pending",
                    "summary": "Awaiting Codex inspection of the deterministic and independently reviewed RDF candidate.",
                    "auditedAt": None,
                    "findings": [],
                    "bindings": {},
                }
                atomic_write_json(candidate / "codex_audit.json", audit)
                _write_candidate_manifest(
                    root=candidate,
                    config=config,
                    state=state,
                    contract=contract,
                    status="awaiting_manual_audit",
                    runtime=state.get("reviewerRuntime"),
                )
                if not config.quality.manual_audit_required:
                    _save_state(workspace, config, state, "awaiting_manual_audit", candidateBundle=str(candidate))
                    return record_codex_audit(
                        config,
                        workspace,
                        run_id=run_id,
                        decision="approve",
                        summary="Manual audit disabled by configuration after development acceptance.",
                        findings=[],
                    )
                _save_state(workspace, config, state, "awaiting_manual_audit", candidateBundle=str(candidate))
                return {"runId": run_id, "status": "awaiting_manual_audit", "bundle": str(candidate), "review": review}
            if stage == "awaiting_manual_audit":
                return {"runId": run_id, "status": "awaiting_manual_audit", "bundle": str(candidate)}
            raise RuntimeError(f"cannot continue Stage 2 from checkpoint stage {stage}")


def record_codex_audit(
    config: Stage2Config,
    workspace: Path,
    *,
    run_id: str,
    decision: str,
    summary: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    if decision not in {"approve", "revise"}:
        raise ValueError("Codex audit decision must be approve or revise")
    workspace = workspace.resolve()
    state = read_json_object(_state_path(workspace, config, run_id))
    if state.get("stage") != "awaiting_manual_audit":
        raise ValueError("Stage 2 run is not awaiting manual audit")
    candidate = Path(str(state["candidateBundle"]))
    stage1_bundle = Path(str(state["stage1Bundle"])) if state.get("stage1Draft") else None
    contract = resolve_stage1_contract(
        config,
        workspace,
        stage1_bundle=stage1_bundle,
        allow_draft=bool(state.get("stage1Draft")),
    )
    binding_names = [
        config.output.nquads_name,
        config.output.ntriples_name,
        config.output.rdfxml_name,
        "extraction_plans.json",
        config.output.validation_name,
        "review.json",
    ]
    audit = {
        "schemaVersion": "dataelf-stage2-codex-audit.v2",
        "decision": decision,
        "summary": summary,
        "auditedAt": utc_now(),
        "findings": findings,
        "bindings": {name: file_sha256(candidate / name) for name in binding_names},
    }
    atomic_write_json(candidate / "codex_audit.json", audit)
    status = "candidate_approved" if decision == "approve" else "manual_revise"
    _write_candidate_manifest(root=candidate, config=config, state=state, contract=contract, status=status)
    if decision == "revise":
        _save_state(workspace, config, state, "manual_revise", auditSummary=summary)
        return {"runId": run_id, "status": "manual_revise", "bundle": str(candidate)}
    if contract.is_draft:
        _save_state(workspace, config, state, "candidate_approved", candidateBundle=str(candidate))
        return {
            "runId": run_id,
            "status": "candidate_approved",
            "bundle": str(candidate),
            "latestUpdated": False,
            "stableRdfUpdated": False,
        }
    target = _promote(workspace=workspace, config=config, state=state, contract=contract, candidate=candidate)
    return {"runId": run_id, "status": "completed", "bundle": str(target), "stableRdf": state["stableRdf"]}


def list_runs(workspace: Path, config: Stage2Config | None = None) -> list[dict[str, Any]]:
    root = stage2_root(workspace.resolve(), config) / ".checkpoints" / "runs"
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/pipeline.json"), reverse=True) if root.exists() else []:
        try:
            result.append(read_json_object(path))
        except ValueError:
            pass
    return result


def validate_published(config: Stage2Config, workspace: Path, bundle: Path | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    if bundle is None:
        latest = read_json_object(stage2_root(workspace, config) / "latest.json")
        bundle = Path(str(latest["bundle"]))
    bundle = bundle.resolve()
    candidate_manifest_path = bundle / "candidate_manifest.json"
    manifest_path = bundle / config.output.manifest_name
    is_candidate = candidate_manifest_path.is_file()
    manifest = read_json_object(candidate_manifest_path if is_candidate else manifest_path)
    hash_errors: list[str] = []
    for name, record in manifest.get("artifacts", {}).items():
        path = bundle / name
        if (
            not path.is_file()
            or not isinstance(record, dict)
            or file_sha256(path) != record.get("sha256")
            or path.stat().st_size != record.get("sizeBytes")
        ):
            hash_errors.append(name)
    validation = read_json_object(bundle / config.output.validation_name)
    serialization_errors, serialization = validate_serialization_files(
        bundle / config.output.nquads_name,
        bundle / config.output.ntriples_name,
        bundle / config.output.rdfxml_name,
    )
    stage1_bundle = Path(str(manifest["stage1Bundle"])) if manifest.get("stage1Draft") else None
    contract_valid = False
    try:
        contract = resolve_stage1_contract(
            config,
            workspace,
            stage1_bundle=stage1_bundle,
            allow_draft=bool(manifest.get("stage1Draft")),
        )
        contract_valid = contract.contract_fingerprint == manifest.get("contractFingerprint")
    except (OSError, ValueError, RuntimeError):
        contract_valid = False
    stable_valid: bool | None = None
    if not is_candidate:
        stable = workspace.parent / f"{workspace.name}{config.output.stable_suffix}"
        stable_valid = stable.is_file() and file_sha256(stable) == file_sha256(bundle / config.output.rdfxml_name)
    valid = (
        not hash_errors
        and not serialization_errors
        and validation.get("status") == "valid"
        and contract_valid
        and stable_valid is not False
    )
    return {
        "schemaVersion": "dataelf-stage2-offline-validation.v2",
        "status": "valid" if valid else "invalid",
        "bundle": str(bundle),
        "candidate": is_candidate,
        "hashErrors": hash_errors,
        "candidateValidation": validation,
        "serializationErrors": serialization_errors,
        "serialization": serialization,
        "stableRdfValid": stable_valid,
        "contractValid": contract_valid,
    }


__all__ = [
    "build",
    "implementation_fingerprint",
    "list_runs",
    "record_codex_audit",
    "validate_published",
]
