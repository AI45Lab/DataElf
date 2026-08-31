from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import append_event, candidate_root, redact
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import ModelConfig, Stage1Config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.domain_adapter import Stage1DomainAdapter


class ModelRuntimeError(RuntimeError):
    pass


class ModelRuntimeTimeout(ModelRuntimeError):
    pass


def _uses_compact_semantic_plan(model: ModelConfig) -> bool:
    return model.provider == "openai" and model.name.strip().lower() == "deepseek-v4-pro"


@dataclass(frozen=True)
class PiRuntime:
    repo: Path
    node: Path
    version: str
    node_version: str


def _node_version(node: Path) -> str:
    result = subprocess.run([str(node), "--version"], check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip().removeprefix("v")


def resolve_pi_runtime(config: Stage1Config) -> PiRuntime:
    repo = config.pi.repo
    package_path = repo / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
    missing = [str(path) for path in (repo, package_path, config.pi.node) if not path.exists()]
    if missing:
        raise FileNotFoundError("Pi runtime is incomplete: " + ", ".join(missing))
    package = json.loads(package_path.read_text(encoding="utf-8"))
    version = str(package.get("version", ""))
    if version != config.pi.supported_version:
        raise ModelRuntimeError(f"Pi version {version!r} differs from required {config.pi.supported_version!r}")
    node_version = _node_version(config.pi.node)
    major = int(node_version.split(".", 1)[0])
    if major < 22:
        raise ModelRuntimeError(f"Pi requires Node >=22, found {node_version}")
    return PiRuntime(
        repo=repo,
        node=config.pi.node,
        version=version,
        node_version=node_version,
    )


def endpoint_metadata(base_url: str) -> dict[str, Any]:
    parts = urlsplit(base_url)
    hostname = parts.hostname or ""
    port = parts.port
    netloc = hostname if port is None else f"{hostname}:{port}"
    sanitized = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    return {"scheme": parts.scheme, "host": hostname, "port": port, "baseUrl": sanitized}


def _runtime_command(runtime: PiRuntime, script: Path, config_path: Path) -> list[str]:
    command = [str(runtime.node), "--experimental-strip-types", "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON"]
    command.extend([str(script), "run", str(config_path)])
    return command


def _parse_envelope(stdout: str, label: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if not line.strip().startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("protocolVersion") == 1:
            if value.get("ok") is not True:
                raise ModelRuntimeError(f"{label} failed: {value.get('error', 'unknown runtime error')}")
            return value
    raise ModelRuntimeError(f"{label} did not emit a valid runtime envelope")


def _execute(
    *,
    runtime: PiRuntime,
    script: Path,
    runtime_config: dict[str, Any],
    runtime_config_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    environment: dict[str, str],
    label: str,
) -> dict[str, Any]:
    atomic_write_json(runtime_config_path, runtime_config)
    command = _runtime_command(runtime, script, runtime_config_path)
    process = subprocess.Popen(
        command,
        cwd=runtime.repo,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.write_text(str(redact(stderr)), encoding="utf-8")
        raise
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.write_text(str(redact(stderr)), encoding="utf-8")
        raise ModelRuntimeTimeout(f"{label} timed out after {timeout_seconds} seconds") from exc
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text(str(redact(stderr)), encoding="utf-8")
    if process.returncode != 0:
        try:
            return _parse_envelope(stdout, label)
        except ModelRuntimeError as envelope_error:
            diagnostic = str(redact(stderr.strip()))[-4000:]
            raise ModelRuntimeError(f"{envelope_error}; diagnostic={diagnostic}") from envelope_error
    return _parse_envelope(stdout, label)


def _environment(model: ModelConfig, runtime_key_name: str) -> tuple[dict[str, str], str]:
    key = os.getenv(model.api_key_env, "").strip()
    base_url = os.getenv(model.base_url_env, "").strip()
    if not key:
        raise ModelRuntimeError(f"environment variable {model.api_key_env} is required")
    if not base_url:
        raise ModelRuntimeError(f"environment variable {model.base_url_env} is required")
    secret_name = re.compile(r"(?:api[_-]?key|authorization|password|secret|token|cookie)", re.I)
    environment = {name: value for name, value in os.environ.items() if not secret_name.search(name)}
    environment[runtime_key_name] = key
    return environment, base_url


def _common_config(model: ModelConfig, base_url: str, system_prompt: str, prompt: str, evidence_path: Path) -> dict[str, Any]:
    stage1_dir = Path(__file__).resolve().parent.parent
    return {
        "provider": model.provider,
        "model": model.name,
        "baseUrl": endpoint_metadata(base_url)["baseUrl"],
        "contextWindow": model.context_window,
        "maxTokens": model.max_tokens,
        "temperature": model.temperature,
        "requestTimeoutSeconds": model.request_timeout_seconds,
        "requestMaxRetries": model.request_max_retries,
        "systemPrompt": system_prompt,
        "prompt": prompt,
        "bridge": {
            "pythonExecutable": sys.executable,
            "path": str(stage1_dir / "tool_bridge.py"),
            "evidencePath": str(evidence_path),
            "maxOutputBytes": 2_000_000,
        },
    }


def _collect_refs(value: Any) -> list[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidenceRef" and isinstance(child, str):
                refs.add(child)
            elif key.endswith("EvidenceRef") and isinstance(child, str):
                refs.add(child)
            elif key.endswith("EvidenceRefs") and isinstance(child, list):
                refs.update(item for item in child if isinstance(item, str))
            refs.update(_collect_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_collect_refs(child))
    return sorted(refs)


def _raw_patterns(value: Any, lineage: dict[str, Any]) -> set[str]:
    patterns: set[str] = set()
    entries = value if isinstance(value, list) else [value]
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        endpoint = str(raw.get("endpoint", ""))
        path = str(raw.get("pathPattern", ""))
        if endpoint and path:
            patterns.add(f"{endpoint}|{path}")
        coordinates: list[str] = []
        if isinstance(raw.get("table"), str) and isinstance(raw.get("column"), str):
            coordinates.append(f"{raw['table']}.{raw['column']}")
        if isinstance(raw.get("table"), str):
            coordinates.extend(f"{raw['table']}.{column}" for column in raw.get("identityColumns", []) if isinstance(column, str))
        if isinstance(raw.get("relationTable"), str):
            coordinates.extend(f"{raw['relationTable']}.{column}" for column in raw.get("sourceColumns", []) if isinstance(column, str))
        if isinstance(raw.get("targetTable"), str):
            coordinates.extend(f"{raw['targetTable']}.{column}" for column in raw.get("targetColumns", []) if isinstance(column, str))
        for coordinate in coordinates:
            mapping = lineage.get("mappings", {}).get(coordinate, {})
            map_endpoint = str(mapping.get("endpoint", ""))
            for raw_path in mapping.get("rawPaths", []):
                if map_endpoint and isinstance(raw_path, str) and raw_path.startswith("/"):
                    patterns.add(f"{map_endpoint}|{raw_path}")
    return patterns


def _controller_column_classifications(
    grounding: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    existing = grounding.get("columnClassifications")
    result = dict(existing) if isinstance(existing, dict) else {}
    lineage = evidence.get("normalizationLineage") or {}
    lineage_ref = evidence.get("normalizationLineageEvidenceRef")
    profile_refs = (evidence.get("toolIndex") or {}).get("profiles", {})
    for table in evidence.get("catalog", {}).get("tables", []):
        table_name = str(table.get("name", ""))
        for column in table.get("columns", []):
            coordinate = f"{table_name}.{column}"
            mapping = (lineage.get("mappings") or {}).get(coordinate, {})
            current = result.get(coordinate)
            unsafe = mapping.get("ontologyEligible") is False
            if not isinstance(current, dict) or unsafe:
                refs = [ref for ref in (profile_refs.get(table_name), lineage_ref) if isinstance(ref, str)]
                reason = (
                    f"Normalization transform {mapping.get('transform', 'omitted')} is not ontology-eligible; "
                    "the value remains losslessly accessible through the Source layer."
                    if unsafe else
                    "Not promoted from the normalized evidence view; the raw value remains accessible through the Source layer."
                )
                result[coordinate] = {
                    "role": "ignored", "propertyIds": [], "reason": reason, "evidenceRefs": refs,
                }
    return result


def _controller_source_bindings(
    candidate: dict[str, Any],
    evidence: dict[str, Any],
    endpoint_targets: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    ontology = candidate.get("ontology") if isinstance(candidate.get("ontology"), dict) else {}
    grounding = candidate.get("grounding") if isinstance(candidate.get("grounding"), dict) else {}
    classes = ontology.get("classes") if isinstance(ontology.get("classes"), dict) else {}
    objects = ontology.get("objectProperties") if isinstance(ontology.get("objectProperties"), dict) else {}
    datatypes = ontology.get("datatypeProperties") if isinstance(ontology.get("datatypeProperties"), dict) else {}
    source_ref = evidence.get("sourceIndexEvidenceRef")
    catalog_ref = evidence.get("catalogEvidenceRef")
    lineage = evidence.get("normalizationLineage") or {}
    evidence_sections = {
        **{key: value for key, value in (grounding.get("classEvidence") or {}).items()},
        **{key: value for key, value in (grounding.get("objectPropertyEvidence") or {}).items()},
        **{key: value for key, value in (grounding.get("datatypePropertyEvidence") or {}).items()},
    }
    all_elements = {**classes, **objects, **datatypes}
    source_access_paths = grounding.get("sourceAccessPaths") if isinstance(grounding.get("sourceAccessPaths"), dict) else {}
    result: dict[str, Any] = {}
    for identifier, element in all_elements.items():
        evidence_value = evidence_sections.get(identifier, [])
        semantic_refs = _collect_refs(evidence_value)
        if not semantic_refs and isinstance(catalog_ref, str):
            semantic_refs = [catalog_ref]
        domain = element.get("domain") if isinstance(element, dict) else None
        access_start = str(domain or identifier)
        access = source_access_paths.get(access_start) if isinstance(source_access_paths.get(access_start), dict) else {}
        access_steps = access.get("steps") if isinstance(access.get("steps"), list) else []
        locator_properties = access.get("locatorPropertyIds") if isinstance(access.get("locatorPropertyIds"), list) else []
        result[identifier] = {
            "semanticEvidenceRefs": semantic_refs,
            "sourceEvidenceRefs": [source_ref] if isinstance(source_ref, str) else [],
            "rawPathPatterns": sorted(_raw_patterns(evidence_value, lineage)),
            "navigation": [f"ontologyElement:{identifier}", f"startClass:{access_start}", *access_steps, *locator_properties],
        }
    profiles = (evidence.get("sourceIndex") or {}).get("pathProfiles", {})
    bound = {pattern for item in result.values() for pattern in item["rawPathPatterns"]}
    for key, profile in profiles.items():
        if not isinstance(profile, dict) or key in bound:
            continue
        if profile.get("classification") not in {"semantic_promoted", "observation_promoted"}:
            continue
        if not str(profile.get("pathPattern", "")).startswith("/data/list/*"):
            continue
        targets = endpoint_targets.get(str(profile.get("endpoint")))
        if not targets:
            continue
        target = targets[1] if profile.get("classification") == "observation_promoted" else targets[0]
        if target in result:
            result[target]["rawPathPatterns"].append(key)
            result[target]["rawPathPatterns"] = sorted(set(result[target]["rawPathPatterns"]))
    return result


def _seed_sections(
    candidate: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
    domain_adapter: Stage1DomainAdapter,
) -> dict[str, Any]:
    evidence = evidence or {}
    sections: dict[str, Any] = {}
    if candidate:
        ontology = candidate.get("ontology")
        grounding = candidate.get("grounding")
        if not isinstance(ontology, dict) or not isinstance(grounding, dict):
            raise ModelRuntimeError("repair baseline must contain ontology and grounding objects")
        for name in ("metadata", "classes", "objectProperties", "datatypeProperties"):
            if name in ontology:
                sections[name] = ontology[name]
        for name in (
            "tableClassifications", "columnClassifications", "classEvidence",
            "objectPropertyEvidence", "datatypePropertyEvidence", "entityObservationMappings",
            "accessPaths", "domainHintResolutions", "competencyQuestions", "cqCoverage", "sourceCoverage",
            "sourceBindings", "sourceAccessPaths", "rawPathClassifications", "associationMappings",
            "entityResolutionMappings", "responseObservationMappings", "relationAuthority",
            "observationValueMappings", "relationSnapshotMappings", "iriGenerationMappings",
            "shaclContract", "normalizationEvidenceRefs",
        ):
            if name in grounding:
                sections[name] = grounding[name]
    # These exhaustive maps are controller-owned.  The model consumes them but
    # cannot accidentally omit or paraphrase raw coverage during generation.
    source_index = evidence.get("sourceIndex")
    source_ref = evidence.get("sourceIndexEvidenceRef")
    if isinstance(source_index, dict) and isinstance(source_ref, str):
        sections["rawPathClassifications"] = {
            key: {
                "endpoint": value.get("endpoint"),
                "pathPattern": value.get("pathPattern"),
                "classification": value.get("classification"),
                "occurrenceCount": value.get("occurrenceCount"),
                "evidenceRefs": [source_ref],
            }
            for key, value in source_index.get("pathProfiles", {}).items()
            if isinstance(value, dict)
        }
        metrics = source_index.get("metrics", {})
        catalog = evidence.get("catalog", {})
        sections["sourceCoverage"] = {
            "tableCount": catalog.get("tableCount"),
            "nonEmptyTableCount": catalog.get("nonEmptyTableCount"),
            "columnCount": sum(len(item.get("columns", [])) for item in catalog.get("tables", [])),
            "totalRowCount": catalog.get("totalRowCount"),
            "documentCount": metrics.get("documentCount"),
            "nonEmptyResponseCount": metrics.get("nonEmptyResponseCount"),
            "emptyResponseCount": metrics.get("emptyResponseCount"),
            "recordObservationCount": metrics.get("recordCount"),
            "fragmentCount": metrics.get("fragmentCount"),
            "rawPathPatternCount": metrics.get("pathPatternCount"),
            "unclassifiedRawPathCount": metrics.get("unclassifiedPathCount"),
            "sourceIndexSha256": source_index.get("sourceIndexSha256"),
            "evidenceRefs": [source_ref],
        }
    lineage_ref = evidence.get("normalizationLineageEvidenceRef")
    if isinstance(lineage_ref, str):
        sections["normalizationEvidenceRefs"] = [lineage_ref]
    if candidate and isinstance(candidate.get("grounding"), dict):
        sections["columnClassifications"] = _controller_column_classifications(candidate["grounding"], evidence)
        sections["sourceBindings"] = _controller_source_bindings(
            candidate, evidence, domain_adapter.source_endpoint_targets
        )
    return sections


def _normalize_singleton_evidence_arrays(candidate: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Normalize a common model-only shape slip without changing semantics.

    The public contract and offline validator remain strict.  This adapter is
    limited to a singleton evidence object where the contract unambiguously
    requires a one-element array; it never invents or drops evidence.
    """
    ontology = candidate.get("ontology")
    grounding = candidate.get("grounding")
    if not isinstance(ontology, dict) or not isinstance(grounding, dict):
        return candidate, 0
    normalized = dict(candidate)
    normalized_grounding = dict(grounding)
    normalized_count = 0
    for section_name in ("classEvidence", "objectPropertyEvidence", "datatypePropertyEvidence"):
        section = grounding.get(section_name)
        if not isinstance(section, dict):
            continue
        normalized_section = dict(section)
        for identifier, entry in section.items():
            if isinstance(entry, dict):
                normalized_section[identifier] = [entry]
                normalized_count += 1
        normalized_grounding[section_name] = normalized_section
    normalized["grounding"] = normalized_grounding
    return normalized, normalized_count


def run_generator(
    *,
    config: Stage1Config,
    workspace: Path,
    run_id: str,
    round_number: int,
    evidence: dict[str, Any],
    evidence_path: Path,
    feedback: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    resume_runtime: bool,
    domain_adapter: Stage1DomainAdapter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = resolve_pi_runtime(config)
    environment, base_url = _environment(config.generator, "ONTOLOGY_STAGE1_API_KEY")
    root = candidate_root(workspace, run_id) / "runtime"
    compact_semantic_plan = _uses_compact_semantic_plan(config.generator)
    runtime_config = _common_config(
        config.generator,
        base_url,
        domain_adapter.generator_system,
        (
            domain_adapter.semantic_plan_prompt(config, evidence, feedback)
            if compact_semantic_plan
            else domain_adapter.generator_prompt(config, evidence, feedback, baseline)
        ),
        evidence_path,
    )
    runtime_config.update(
        {
            "piVersion": runtime.version,
            "sourceFingerprint": evidence["sourceFingerprint"],
            "stageStatePath": str(root / f"generator_round_{round_number}_staged.json"),
            "modelEventLogPath": str(root / f"generator_round_{round_number}_model_events.jsonl"),
            "resume": resume_runtime,
            "seedSections": _seed_sections(baseline, evidence, domain_adapter),
        }
    )
    append_event(
        workspace,
        run_id,
        "generator_process_start",
        round=round_number,
        model=config.generator.name,
        endpoint=endpoint_metadata(base_url),
        piVersion=runtime.version,
        nodeVersion=runtime.node_version,
        processTimeoutSeconds=config.generator.process_timeout_seconds,
        requestTimeoutSeconds=config.generator.request_timeout_seconds,
        requestMaxRetries=config.generator.request_max_retries,
        resumed=resume_runtime,
    )
    envelope = _execute(
        runtime=runtime,
        script=Path(__file__).resolve().parent.parent / "runtime" / "pi_runtime.ts",
        runtime_config=runtime_config,
        runtime_config_path=root / f"generator_round_{round_number}_config.json",
        stderr_path=root / f"generator_round_{round_number}.stderr.log",
        timeout_seconds=config.generator.process_timeout_seconds,
        environment=environment,
        label="ontology generator",
    )
    candidate = envelope.get("candidate")
    if not isinstance(candidate, dict):
        raise ModelRuntimeError("generator envelope has no candidate object")
    if candidate.get("schemaVersion") == "dataelf-semantic-plan.v1":
        plan = candidate.get("semanticPlan")
        if not isinstance(plan, dict):
            raise ModelRuntimeError("semantic-plan envelope has no semanticPlan object")
        try:
            candidate = domain_adapter.candidate_from_semantic_plan(plan, config, evidence["sourceFingerprint"])
        except ValueError as exc:
            raise ModelRuntimeError(f"invalid compact semantic plan: {exc}") from exc
        candidate["grounding"].update(_seed_sections(None, evidence, domain_adapter))
    candidate, normalization_count = _normalize_singleton_evidence_arrays(candidate)
    candidate, contract_normalization = domain_adapter.normalize_candidate_contract(candidate, evidence, config)
    metadata = {
        "provider": config.generator.provider,
        "model": config.generator.name,
        "endpoint": endpoint_metadata(base_url),
        "piVersion": runtime.version,
        "nodeVersion": runtime.node_version,
        "processTimeoutSeconds": config.generator.process_timeout_seconds,
        "requestTimeoutSeconds": config.generator.request_timeout_seconds,
        "requestMaxRetries": config.generator.request_max_retries,
        "controllerSingletonEvidenceNormalizations": normalization_count,
        "controllerContractNormalization": contract_normalization,
    }
    return candidate, metadata


def run_reviewer(
    *,
    config: Stage1Config,
    workspace: Path,
    run_id: str,
    round_number: int,
    evidence: dict[str, Any],
    evidence_path: Path,
    candidate: dict[str, Any],
    validation: dict[str, Any],
    domain_adapter: Stage1DomainAdapter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = resolve_pi_runtime(config)
    environment, base_url = _environment(config.reviewer, "ONTOLOGY_STAGE1_REVIEWER_API_KEY")
    root = candidate_root(workspace, run_id) / "runtime"
    runtime_config = _common_config(
        config.reviewer,
        base_url,
        domain_adapter.reviewer_system,
        (
            domain_adapter.compact_reviewer_prompt(config, evidence, candidate, validation)
            if _uses_compact_semantic_plan(config.reviewer)
            else domain_adapter.reviewer_prompt(config, evidence, candidate, validation)
        ),
        evidence_path,
    )
    runtime_config.update(
        {
            "piVersion": runtime.version,
            "modelEventLogPath": str(root / f"reviewer_round_{round_number}_model_events.jsonl"),
        }
    )
    append_event(
        workspace,
        run_id,
        "reviewer_process_start",
        round=round_number,
        model=config.reviewer.name,
        endpoint=endpoint_metadata(base_url),
        piVersion=runtime.version,
        nodeVersion=runtime.node_version,
        processTimeoutSeconds=config.reviewer.process_timeout_seconds,
        requestTimeoutSeconds=config.reviewer.request_timeout_seconds,
        requestMaxRetries=config.reviewer.request_max_retries,
        freshContext=True,
    )
    envelope = _execute(
        runtime=runtime,
        script=Path(__file__).resolve().parent.parent / "runtime" / "reviewer_runtime.ts",
        runtime_config=runtime_config,
        runtime_config_path=root / f"reviewer_round_{round_number}_config.json",
        stderr_path=root / f"reviewer_round_{round_number}.stderr.log",
        timeout_seconds=config.reviewer.process_timeout_seconds,
        environment=environment,
        label="ontology reviewer",
    )
    review = envelope.get("review")
    if not isinstance(review, dict):
        raise ModelRuntimeError("reviewer envelope has no review object")
    metadata = {
        "provider": config.reviewer.provider,
        "model": config.reviewer.name,
        "endpoint": endpoint_metadata(base_url),
        "piVersion": runtime.version,
        "nodeVersion": runtime.node_version,
        "processTimeoutSeconds": config.reviewer.process_timeout_seconds,
        "requestTimeoutSeconds": config.reviewer.request_timeout_seconds,
        "requestMaxRetries": config.reviewer.request_max_retries,
        "freshContext": True,
    }
    return review, metadata
