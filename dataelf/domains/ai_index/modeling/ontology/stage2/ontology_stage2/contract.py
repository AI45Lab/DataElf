from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import file_sha256, read_json_object, sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config


class ContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stage1Contract:
    workspace: Path
    run_id: str
    bundle: Path
    is_draft: bool
    ontology: dict[str, Any]
    grounding: dict[str, Any]
    validation: dict[str, Any]
    manifest: dict[str, Any]
    artifact_hashes: dict[str, str]
    source_index: dict[str, Any]
    source_index_path: Path
    raw_documents: dict[str, dict[str, Any]]
    shacl_path: Path | None
    contract_fingerprint: str

    @property
    def source_fingerprint(self) -> str:
        return str(self.grounding["sourceFingerprint"])


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit():
                raise KeyError(pointer)
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise KeyError(pointer)
    return current


def _artifact_records(manifest: dict[str, Any]) -> dict[str, Any]:
    records = manifest.get("artifacts")
    if not isinstance(records, dict):
        raise ContractError("Stage 1 manifest.artifacts must be an object")
    return records


def _verify_artifacts(bundle: Path, manifest: dict[str, Any], *, draft: bool) -> dict[str, str]:
    records = _artifact_records(manifest)
    required = {"ontology.json", "grounding.json", "validation.json"}
    if draft:
        required |= {"shacl.ttl", "codex_audit.json"}
    else:
        required |= {"review.json", "source_index.json"}
        if manifest.get("manualAuditRequired") is True:
            required.add("codex_audit.json")
    missing = required - set(records)
    if missing:
        raise ContractError(f"Stage 1 manifest is missing required artifacts: {sorted(missing)}")
    result: dict[str, str] = {}
    for name, record in sorted(records.items()):
        if Path(name).name != name or not isinstance(record, dict):
            raise ContractError(f"invalid Stage 1 artifact record: {name}")
        path = bundle / name
        if not path.is_file():
            raise ContractError(f"Stage 1 artifact is missing: {name}")
        digest = file_sha256(path)
        if digest != record.get("sha256") or path.stat().st_size != record.get("sizeBytes"):
            raise ContractError(f"Stage 1 artifact hash/size mismatch: {name}")
        result[name] = digest
    return result


def _resolve_bundle(
    workspace: Path,
    stage1_bundle: Path | None,
    allow_draft: bool,
) -> tuple[Path, bool, str, dict[str, Any]]:
    root = workspace / "ontology" / "stage1"
    if stage1_bundle is None:
        latest_path = root / "latest.json"
        if not latest_path.is_file():
            raise ContractError(f"Stage 1 latest.json is missing: {latest_path}")
        latest = read_json_object(latest_path)
        bundle = Path(str(latest.get("bundle", ""))).resolve()
        run_id = str(latest.get("runId", ""))
        if not run_id or not bundle.is_dir() or not _inside(bundle, root / "published"):
            raise ContractError("Stage 1 latest bundle is invalid or outside the workspace")
        manifest = read_json_object(bundle / "manifest.json")
        return bundle, False, run_id, manifest
    bundle = stage1_bundle.expanduser().resolve()
    if not allow_draft:
        raise ContractError("--stage1-bundle requires --allow-draft for a Stage 1 candidate")
    if not bundle.is_dir() or not _inside(bundle, root / "candidates"):
        raise ContractError("draft Stage 1 bundle must be inside this workspace's stage1/candidates")
    manifest = read_json_object(bundle / "candidate_manifest.json")
    run_id = str(manifest.get("runId") or bundle.parent.name)
    return bundle, True, run_id, manifest


def _source_index_path(workspace: Path, bundle: Path, fingerprint: str) -> Path:
    bundled = bundle / "source_index.json"
    if bundled.is_file():
        return bundled
    cached = workspace / "ontology" / "stage1" / "source_cache" / fingerprint / "source_index.json"
    if cached.is_file():
        return cached
    raise ContractError("Stage 1 source_index.json is absent from both bundle and fingerprinted source cache")


def _verify_source_index(
    workspace: Path,
    source_index: dict[str, Any],
    fingerprint: str,
) -> dict[str, dict[str, Any]]:
    if source_index.get("schemaVersion") != "dataelf-source-index.v2":
        raise ContractError("Stage 1 source index schema is not v2")
    if source_index.get("sourceFingerprint") != fingerprint:
        raise ContractError("Stage 1 source index fingerprint differs from grounding")
    expected_index_hash = sha256_json({k: v for k, v in source_index.items() if k != "sourceIndexSha256"})
    if source_index.get("sourceIndexSha256") != expected_index_hash:
        raise ContractError("Stage 1 source index canonical hash is invalid")
    documents = source_index.get("documents")
    records = source_index.get("records")
    fragments = source_index.get("fragments")
    if not all(isinstance(value, list) for value in (documents, records, fragments)):
        raise ContractError("Stage 1 source index document/record/fragment sections must be arrays")
    raw: dict[str, dict[str, Any]] = {}
    for document in documents:
        if not isinstance(document, dict):
            raise ContractError("source index document entry must be an object")
        document_id = str(document.get("documentId", ""))
        relative = Path(str(document.get("relativeFile", "")))
        path = (workspace / relative).resolve()
        if not document_id or relative.is_absolute() or ".." in relative.parts or not _inside(path, workspace):
            raise ContractError(f"unsafe source document locator: {relative}")
        if not path.is_file() or file_sha256(path) != document.get("sha256") or path.stat().st_size != document.get("sizeBytes"):
            raise ContractError(f"raw source hash/size mismatch: {relative}")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot parse raw source {relative}: {exc}") from exc
        if not isinstance(envelope, dict) or envelope.get("endpoint") != document.get("endpoint"):
            raise ContractError(f"raw source endpoint mismatch: {relative}")
        raw[document_id] = envelope
    record_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ContractError("source index record entry must be an object")
        record_id = str(record.get("recordId", ""))
        document_id = str(record.get("documentId", ""))
        try:
            value = _pointer(raw[document_id], str(record.get("jsonPointer", "")))
        except (KeyError, IndexError) as exc:
            raise ContractError(f"source record pointer cannot replay: {record_id}") from exc
        if not record_id or sha256_json(value) != record.get("recordHash"):
            raise ContractError(f"source record hash mismatch: {record_id}")
        record_ids.add(record_id)
    for fragment in fragments:
        if not isinstance(fragment, dict):
            raise ContractError("source index fragment entry must be an object")
        fragment_id = str(fragment.get("fragmentId", ""))
        document_id = str(fragment.get("documentId", ""))
        record_id = fragment.get("recordId")
        if record_id is not None and record_id not in record_ids:
            raise ContractError(f"source fragment references unknown record: {fragment_id}")
        try:
            value = _pointer(raw[document_id], str(fragment.get("jsonPointer", "")))
        except (KeyError, IndexError) as exc:
            raise ContractError(f"source fragment pointer cannot replay: {fragment_id}") from exc
        if sha256_json(value) != fragment.get("valueHash"):
            raise ContractError(f"source fragment value hash mismatch: {fragment_id}")
        if fragment.get("classification") not in {
            "semantic_promoted",
            "observation_promoted",
            "derived_with_formula",
            "redundant_but_source_linked",
            "source_only",
        }:
            raise ContractError(f"source fragment has invalid classification: {fragment_id}")
    metrics = source_index.get("metrics") or {}
    actual = {
        "documentCount": len(documents),
        "recordCount": len(records),
        "fragmentCount": len(fragments),
        "emptyResponseCount": sum(bool(item.get("empty")) for item in documents),
    }
    for key, value in actual.items():
        if metrics.get(key) != value:
            raise ContractError(f"source index metric {key}={metrics.get(key)!r}, expected {value}")
    if metrics.get("unclassifiedPathCount") != 0:
        raise ContractError("source index contains unclassified raw paths")
    return raw


def resolve_stage1_contract(
    config: Stage2Config,
    workspace: Path,
    *,
    stage1_bundle: Path | None = None,
    allow_draft: bool = False,
) -> Stage1Contract:
    workspace = workspace.resolve()
    bundle, is_draft, run_id, manifest = _resolve_bundle(workspace, stage1_bundle, allow_draft)
    artifact_hashes = _verify_artifacts(bundle, manifest, draft=is_draft)
    ontology = read_json_object(bundle / "ontology.json")
    grounding = read_json_object(bundle / "grounding.json")
    validation = read_json_object(bundle / "validation.json")
    if ontology.get("schemaVersion") != "dataelf-ontology.v2" or grounding.get("schemaVersion") != "dataelf-grounding.v2":
        raise ContractError("Stage 1 ontology/grounding must use the v2 contract")
    if validation.get("status") != "valid" or validation.get("errors"):
        raise ContractError("Stage 1 deterministic validation is not valid")
    fingerprint = str(grounding.get("sourceFingerprint", ""))
    if not fingerprint or ontology.get("metadata", {}).get("sourceFingerprint") != fingerprint:
        raise ContractError("Stage 1 ontology/grounding source fingerprints differ")
    audit_path = bundle / "codex_audit.json"
    if is_draft or manifest.get("manualAuditRequired") is True or audit_path.is_file():
        audit = read_json_object(audit_path)
        allowed_audits = {"approve_for_independent_review"} if is_draft else {"approve"}
        if audit.get("decision") not in allowed_audits or audit.get("findings"):
            raise ContractError("Stage 1 Codex audit is not acceptable for this contract mode")
    if not is_draft and config.quality.require_reviewer_approve:
        review = read_json_object(bundle / "review.json")
        blocking = [
            issue
            for issue in review.get("issues", [])
            if isinstance(issue, dict) and issue.get("severity") in config.quality.blocking_severities
        ]
        if review.get("verdict") != "approve" or blocking:
            raise ContractError("published Stage 1 reviewer decision is not an unblocked approval")
    source_index_path = _source_index_path(workspace, bundle, fingerprint)
    source_index = read_json_object(source_index_path)
    raw_documents = _verify_source_index(workspace, source_index, fingerprint)
    shacl_path = bundle / "shacl.ttl"
    if not shacl_path.is_file():
        shacl_path = None
    contract_fingerprint = sha256_json(
        {
            "draft": is_draft,
            "runId": run_id,
            "sourceFingerprint": fingerprint,
            "sourceIndexSha256": source_index.get("sourceIndexSha256"),
            "ontologySha256": artifact_hashes["ontology.json"],
            "groundingSha256": artifact_hashes["grounding.json"],
            "validationSha256": artifact_hashes["validation.json"],
            "shaclSha256": file_sha256(shacl_path) if shacl_path else None,
        }
    )
    return Stage1Contract(
        workspace=workspace,
        run_id=run_id,
        bundle=bundle,
        is_draft=is_draft,
        ontology=ontology,
        grounding=grounding,
        validation=validation,
        manifest=manifest,
        artifact_hashes=artifact_hashes,
        source_index=source_index,
        source_index_path=source_index_path,
        raw_documents=raw_documents,
        shacl_path=shacl_path,
        contract_fingerprint=contract_fingerprint,
    )


__all__ = ["ContractError", "Stage1Contract", "resolve_stage1_contract", "_pointer"]
