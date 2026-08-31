from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, file_sha256, read_json_object, sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import utc_now
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import Stage1Contract, resolve_stage1_contract
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.prompts import compiler_prompt


PLAN_VERSION = "dataelf-stage2-extraction-plan.v2"
COMPILE_VERSION = "dataelf-stage2-compile-manifest.v2"
ALLOWED_OPS = {
    "copy_scalar",
    "explode_array",
    "object_fields",
    "reference_by_business_id",
    "concept_by_normalized_value",
    "reify_array_membership",
    "derived_formula",
    "require_consensus",
    "authority_projection",
}
ENDPOINT_SLUGS = {
    "/openapi/paper/search": "paper",
    "/openapi/scholar/search": "scholar",
    "/openapi/institutions/search": "institution",
}


ModelRunner = Callable[[Stage2Config, Stage1Contract, str, dict[str, Any], str, Path], dict[str, Any]]


def compile_fingerprints(config: Stage2Config, contract: Stage1Contract) -> dict[str, Any]:
    stage2_dir = Path(__file__).resolve().parents[1]
    code_paths = [
        Path(__file__).resolve(),
        Path(__file__).with_name("contract.py").resolve(),
        Path(__file__).with_name("model_runtime.py").resolve(),
        Path(__file__).with_name("prompts.py").resolve(),
    ]
    components = {
        "stage1Contract": contract.contract_fingerprint,
        "stage1Artifacts": dict(sorted(contract.artifact_hashes.items())),
        "source": contract.source_fingerprint,
        "sourceIndex": str(contract.source_index["sourceIndexSha256"]),
        "prompt": file_sha256(Path(__file__).with_name("prompts.py")),
        "schema": sha256_json({"planVersion": PLAN_VERSION, "operations": sorted(ALLOWED_OPS)}),
        "code": sha256_json({path.name: file_sha256(path) for path in code_paths}),
        "runtime": sha256_json(
            {
                "transport": "openai_json",
            }
        ),
        "model": sha256_json(
            {
                "provider": config.compiler.provider,
                "name": config.compiler.name,
                "contextWindow": config.compiler.context_window,
                "maxTokens": config.compiler.max_tokens,
                "temperature": config.compiler.temperature,
                "requestTimeoutSeconds": config.compiler.request_timeout_seconds,
                "requestMaxRetries": config.compiler.request_max_retries,
                "baseUrlEnv": config.compiler.base_url_env,
            }
        ),
        "config": file_sha256(config.path),
    }
    return {**components, "aggregate": sha256_json(components)}


def stage2_root(workspace: Path, config: Stage2Config | None = None) -> Path:
    subdir = config.output.artifacts_subdir if config else "ontology/stage2"
    return workspace / subdir


def compiled_root(contract: Stage1Contract, config: Stage2Config) -> Path:
    return stage2_root(contract.workspace, config) / "compiled" / contract.contract_fingerprint


def _relative_record_path(endpoint_path: str) -> str:
    prefix = "/data/list/*"
    if endpoint_path == prefix:
        return ""
    if not endpoint_path.startswith(prefix + "/"):
        raise ValueError(f"path is not below the endpoint record: {endpoint_path}")
    return endpoint_path.removeprefix(prefix)


def _operation_id(*parts: str) -> str:
    return "op_" + sha256_json(list(parts))[:16]


def _coverage_key(operation: dict[str, Any]) -> str:
    stable = {key: value for key, value in operation.items() if key not in {"id", "coverageKey", "rationale"}}
    return f"{operation['op']}:{sha256_json(stable)[:20]}"


def _add(operations: list[dict[str, Any]], operation: dict[str, Any]) -> None:
    operation = dict(operation)
    operation["coverageKey"] = _coverage_key(operation)
    operation["id"] = _operation_id(operation["op"], operation["coverageKey"])
    if operation["coverageKey"] not in {item["coverageKey"] for item in operations}:
        operations.append(operation)


def _endpoint_mapping(contract: Stage1Contract, endpoint: str) -> tuple[str, str, dict[str, Any]]:
    for entity_class, mapping in contract.grounding.get("entityObservationMappings", {}).items():
        if isinstance(mapping, dict) and mapping.get("endpoint") == endpoint:
            return str(entity_class), str(mapping["observationClassId"]), mapping
    raise ValueError(f"Stage 1 has no entity observation mapping for {endpoint}")


def _path_profiles(contract: Stage1Contract, endpoint: str) -> dict[str, Any]:
    profiles = contract.source_index.get("pathProfiles", {})
    return {
        key: value
        for key, value in profiles.items()
        if isinstance(value, dict) and value.get("endpoint") == endpoint
    }


def build_seed_plan(contract: Stage1Contract, endpoint: str) -> dict[str, Any]:
    if endpoint not in ENDPOINT_SLUGS:
        raise ValueError(f"unsupported endpoint: {endpoint}")
    ontology = contract.ontology
    grounding = contract.grounding
    entity_class, observation_class, entity_mapping = _endpoint_mapping(contract, endpoint)
    operations: list[dict[str, Any]] = []
    observed_property_ids: set[str] = set()

    for mapping_id, mapping in sorted(grounding.get("observationValueMappings", {}).items()):
        if not isinstance(mapping, dict) or mapping.get("endpoint") != endpoint:
            continue
        path = _relative_record_path(str(mapping["pathPattern"]))
        observed = str(mapping["observationPropertyId"])
        canonical = str(mapping["canonicalPropertyId"])
        observed_property_ids.add(observed)
        _add(
            operations,
            {
                "op": "copy_scalar",
                "sourcePath": path,
                "subjectRole": "observation",
                "propertyId": observed,
                "contractMapping": f"observationValueMappings.{mapping_id}",
            },
        )
        _add(
            operations,
            {
                "op": "require_consensus",
                "sourcePath": path,
                "subjectRole": "entity",
                "propertyId": canonical,
                "observationPropertyId": observed,
                "conflictDisposition": "observation_only",
                "contractMapping": f"observationValueMappings.{mapping_id}",
            },
        )

    datatypes = ontology.get("datatypeProperties", {})
    source_bindings = grounding.get("sourceBindings", {})
    controller_properties = {
        "resultRank",
        "recordHash",
        "sourceSha256",
        "sourcePath",
        "recordJsonPointer",
        "jsonPointer",
        "sourceSystem",
        "fragmentValueKind",
        "fragmentValueHash",
        str(entity_mapping.get("sourceRawPropertyId", "")),
    }
    for property_id, definition in sorted(datatypes.items()):
        if property_id in observed_property_ids or property_id in controller_properties:
            continue
        if definition.get("domain") not in {"EntityObservation", observation_class}:
            continue
        binding = source_bindings.get(property_id, {})
        for raw in binding.get("rawPathPatterns", []) if isinstance(binding, dict) else []:
            prefix = endpoint + "|"
            if not isinstance(raw, str) or not raw.startswith(prefix):
                continue
            endpoint_path = raw.removeprefix(prefix)
            try:
                path = _relative_record_path(endpoint_path)
            except ValueError:
                continue
            if "*" in path:
                continue
            _add(
                operations,
                {
                    "op": "copy_scalar",
                    "sourcePath": path,
                    "subjectRole": "observation",
                    "propertyId": property_id,
                    "contractMapping": f"sourceBindings.{property_id}",
                },
            )

    label_properties = {"Topic": "topicName", "Venue": "venueName", "Award": "awardName"}
    id_properties = {"Paper": "paperId", "Scholar": "scholarId", "Institution": "institutionId"}
    for mapping_id, mapping in sorted(grounding.get("relationSnapshotMappings", {}).items()):
        if not isinstance(mapping, dict):
            continue
        for source in mapping.get("sourcePaths", []):
            if not isinstance(source, dict) or source.get("endpoint") != endpoint:
                continue
            path = _relative_record_path(str(source["pathPattern"]))
            target_class = str(mapping["targetClassId"])
            base = {
                "sourcePath": path,
                "subjectRole": "observation",
                "observationPropertyId": str(mapping["observationPropertyId"]),
                "domainShortcutPropertyId": mapping.get("domainShortcutPropertyId"),
                "targetClassId": target_class,
                "contractMapping": f"relationSnapshotMappings.{mapping_id}",
            }
            if target_class in label_properties:
                _add(
                    operations,
                    {
                        "op": "concept_by_normalized_value",
                        **base,
                        "labelPropertyId": label_properties[target_class],
                    },
                )
            elif target_class == "NewsItem":
                _add(
                    operations,
                    {
                        "op": "object_fields",
                        **base,
                        "fieldProperties": {"title": "newsTitle", "source": "newsSource", "date": "newsDate"},
                    },
                )
            elif target_class == "Authorship":
                association = grounding.get("associationMappings", {}).get("Authorship", {})
                _add(
                    operations,
                    {
                        "op": "reify_array_membership",
                        **base,
                        "memberClassId": "Scholar",
                        "membershipClassId": "Authorship",
                        "paperPropertyId": str(association.get("endpointPropertyIds", ["authorshipOfPaper"])[0]),
                        "memberPropertyId": str(association.get("endpointPropertyIds", ["", "authoredByScholar"])[1]),
                        "inverseObservationPropertyId": mapping.get("inversePropertyId"),
                        "qualifiers": association.get("qualifiers", {}),
                        "shortcutPropertyId": "authoredBy",
                        "entityMembershipPropertyId": "hasAuthorship",
                    },
                )
            else:
                _add(
                    operations,
                    {
                        "op": "reference_by_business_id",
                        **base,
                        "targetIdPropertyId": id_properties[target_class],
                    },
                )

    for relation_key, authority in sorted(grounding.get("relationAuthority", {}).items()):
        if not isinstance(authority, dict):
            continue
        raw_authority = str(authority.get("authority", ""))
        authority_endpoint, separator, authority_path = raw_authority.partition(":")
        if not separator or authority_endpoint != endpoint:
            continue
        _add(
            operations,
            {
                "op": "authority_projection",
                "sourcePath": _relative_record_path(authority_path),
                "relationKey": relation_key,
                "differenceStrategy": str(authority.get("differenceStrategy", "")),
                "contractMapping": f"relationAuthority.{relation_key}",
            },
        )

    documents = [item for item in contract.source_index["documents"] if item.get("endpoint") == endpoint]
    records = [item for item in contract.source_index["records"] if contract.raw_documents[str(item["documentId"])].get("endpoint") == endpoint]
    plan = {
        "schemaVersion": PLAN_VERSION,
        "endpoint": endpoint,
        "endpointSlug": ENDPOINT_SLUGS[endpoint],
        "recordPathPattern": "/data/list/*",
        "entity": {
            "classId": entity_class,
            "observationClassId": observation_class,
            "businessIdPath": "/id",
            "observedEntityPropertyId": entity_mapping["observedEntityPropertyId"],
            "genericObservedEntityPropertyId": entity_mapping["genericObservedEntityPropertyId"],
            "sourceRawPropertyId": entity_mapping["sourceRawPropertyId"],
        },
        "operations": sorted(operations, key=lambda item: item["coverageKey"]),
        "expectedSourceCounts": {
            "documentCount": len(documents),
            "emptyResponseCount": sum(bool(item.get("empty")) for item in documents),
            "recordCount": len(records),
        },
        "contractFingerprint": contract.contract_fingerprint,
        "sourceFingerprint": contract.source_fingerprint,
        "sourceIndexSha256": contract.source_index["sourceIndexSha256"],
        "stage1RunId": contract.run_id,
        "compilerRationale": "Controller seed generated from the reviewed Stage 1 grounding contract.",
    }
    plan["planSha256"] = sha256_json(plan)
    return plan


def endpoint_profile(contract: Stage1Contract, endpoint: str) -> dict[str, Any]:
    profiles = _path_profiles(contract, endpoint)
    return {
        "documents": [
            {
                key: item.get(key)
                for key in ("documentId", "relativeFile", "resultCount", "empty", "sha256")
            }
            for item in contract.source_index["documents"]
            if item.get("endpoint") == endpoint
        ],
        # Compiler evidence deliberately excludes pointer samples and repeated
        # document ID arrays.  Those remain controller-validated in source_index;
        # the model only needs path existence, type, classification, and count.
        "pathProfiles": [
            {
                "pathPattern": value.get("pathPattern"),
                "classification": value.get("classification"),
                "occurrenceCount": value.get("occurrenceCount"),
                "valueKinds": value.get("valueKinds", []),
                "documentCount": value.get("documentCount"),
            }
            for _key, value in sorted(profiles.items())
        ],
        "relationComparisons": {
            key: value
            for key, value in contract.source_index.get("relationComparisons", {}).items()
            if isinstance(value, dict) and str(value.get("authority", "")).startswith(endpoint + ":")
        },
    }


def validate_plan(plan: dict[str, Any], contract: Stage1Contract, endpoint: str) -> list[str]:
    errors: list[str] = []
    seed = build_seed_plan(contract, endpoint)
    for key in (
        "schemaVersion",
        "endpoint",
        "recordPathPattern",
        "entity",
        "expectedSourceCounts",
        "contractFingerprint",
        "sourceFingerprint",
        "sourceIndexSha256",
        "stage1RunId",
    ):
        if plan.get(key) != seed.get(key):
            errors.append(f"{key} differs from the controller contract")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        return errors + ["operations must be an array"]
    actual: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            errors.append(f"operations[{index}] must be an object")
            continue
        if operation.get("op") not in ALLOWED_OPS:
            errors.append(f"operations[{index}] uses unsupported op {operation.get('op')!r}")
        if any(key in operation for key in ("value", "default", "code", "python", "iri", "triples")):
            errors.append(f"operations[{index}] contains a forbidden executable or asserted value")
        coverage = operation.get("coverageKey")
        if not isinstance(coverage, str) or coverage in actual:
            errors.append(f"operations[{index}] has missing/duplicate coverageKey")
            continue
        actual[coverage] = operation
    expected = {item["coverageKey"]: item for item in seed["operations"]}
    if set(actual) != set(expected):
        errors.append(
            f"operation coverage differs: missing={sorted(set(expected)-set(actual))}, extra={sorted(set(actual)-set(expected))}"
        )
    for coverage in sorted(set(actual) & set(expected)):
        clean_actual = {k: v for k, v in actual[coverage].items() if k != "rationale"}
        if clean_actual != expected[coverage]:
            errors.append(f"operation {coverage} differs from its Stage 1-bound controller definition")
    expected_hash = sha256_json({key: value for key, value in plan.items() if key != "planSha256"})
    if plan.get("planSha256") != expected_hash:
        errors.append("planSha256 is invalid")
    return errors


def _normalize_model_plan(candidate: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
    submitted = candidate.get("plan") if isinstance(candidate.get("plan"), dict) else candidate
    result = dict(seed)
    accepted = submitted.get("acceptedCoverageKeys")
    if isinstance(accepted, list) and all(isinstance(item, str) for item in accepted):
        selected = set(accepted)
        result["operations"] = [item for item in seed["operations"] if item["coverageKey"] in selected]
    elif isinstance(submitted.get("operations"), list):
        result["operations"] = submitted["operations"]
    if isinstance(submitted.get("compilerRationale"), str):
        result["compilerRationale"] = submitted["compilerRationale"][:4000]
    result["planSha256"] = sha256_json({key: value for key, value in result.items() if key != "planSha256"})
    return result


def _default_model_runner(
    config: Stage2Config,
    contract: Stage1Contract,
    endpoint: str,
    seed: dict[str, Any],
    prompt: str,
    runtime_root: Path,
) -> dict[str, Any]:
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.model_runtime import run_compiler

    return run_compiler(
        config=config,
        contract=contract,
        endpoint=endpoint,
        seed=seed,
        prompt=prompt,
        runtime_root=runtime_root,
    )


def compile_plan(
    config: Stage2Config,
    workspace: Path,
    *,
    replace: bool = False,
    stage1_bundle: Path | None = None,
    allow_draft: bool = False,
    model_runner: ModelRunner | None = None,
    feedback: dict[str, Any] | None = None,
    affected_endpoints: set[str] | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    contract = resolve_stage1_contract(
        config,
        workspace,
        stage1_bundle=stage1_bundle,
        allow_draft=allow_draft,
    )
    root = output_root or compiled_root(contract, config)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "compile_manifest.json"
    previous_manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            previous_manifest = read_json_object(manifest_path)
        except (OSError, ValueError):
            previous_manifest = {}
    fingerprints = compile_fingerprints(config, contract)
    cache_compatible = previous_manifest.get("compileFingerprint") == fingerprints["aggregate"]
    runner = model_runner or _default_model_runner
    results: dict[str, Any] = {}
    model_calls = 0
    endpoints = set(ENDPOINT_SLUGS) if affected_endpoints is None else affected_endpoints
    for endpoint, slug in ENDPOINT_SLUGS.items():
        path = root / f"{slug}.plan.json"
        if endpoint not in endpoints and path.is_file():
            plan = read_json_object(path)
            errors = validate_plan(plan, contract, endpoint)
            if errors:
                raise ValueError(f"preserved {slug} plan is invalid: {'; '.join(errors)}")
            results[endpoint] = {"path": str(path), "planSha256": plan["planSha256"], "reused": True}
            continue
        if path.is_file() and not replace and feedback is None and cache_compatible:
            plan = read_json_object(path)
            errors = validate_plan(plan, contract, endpoint)
            if not errors:
                metadata_path = root / "runtime" / slug / "attempt_00" / "runtime_metadata.json"
                previous_plan = previous_manifest.get("plans", {}).get(endpoint, {})
                results[endpoint] = {
                    "path": str(path),
                    "planSha256": plan["planSha256"],
                    "reused": True,
                    **{
                        key: previous_plan[key]
                        for key in ("promptSha256", "seedPlanSha256", "endpointProfileSha256")
                        if key in previous_plan
                    },
                    **({"runtime": read_json_object(metadata_path)} if metadata_path.is_file() else {}),
                }
                continue
        seed = build_seed_plan(contract, endpoint)
        profile = endpoint_profile(contract, endpoint)
        # An acquired endpoint can be a valid, provenance-bearing empty
        # response.  In that case there are no record extraction operations
        # for a model to accept.  Persist the controller seed directly instead
        # of sending an impossible JSON schema with minItems=maxItems=0 to the
        # compiler runtime (which also correctly rejects missing coverage keys
        # on non-empty seeds).
        if not seed["operations"]:
            atomic_write_json(path, seed)
            results[endpoint] = {
                "path": str(path),
                "planSha256": seed["planSha256"],
                "reused": False,
                "attempt": None,
                "compilerMode": "controller_empty_endpoint",
                "seedPlanSha256": str(seed["planSha256"]),
                "endpointProfileSha256": sha256_json(profile),
            }
            continue
        last_errors: list[str] = []
        for attempt in range(config.quality.max_repair_rounds):
            repair = feedback if attempt == 0 else {"validationErrors": last_errors}
            prompt = compiler_prompt(endpoint=endpoint, seed=seed, endpoint_profile=profile, feedback=repair)
            candidate = runner(config, contract, endpoint, seed, prompt, root / "runtime" / slug / f"attempt_{attempt:02d}")
            model_calls += 1
            plan = _normalize_model_plan(candidate, seed)
            last_errors = validate_plan(plan, contract, endpoint)
            if not last_errors:
                atomic_write_json(path, plan)
                results[endpoint] = {
                    "path": str(path),
                    "planSha256": plan["planSha256"],
                    "reused": False,
                    "attempt": attempt,
                    "promptSha256": sha256_json(prompt),
                    "seedPlanSha256": str(seed["planSha256"]),
                    "endpointProfileSha256": sha256_json(profile),
                    **({"runtime": candidate["_runtime"]} if isinstance(candidate.get("_runtime"), dict) else {}),
                }
                break
        else:
            raise ValueError(f"model could not compile a valid {endpoint} plan: {'; '.join(last_errors)}")
    try:
        previous_model_calls = int(previous_manifest.get("modelCallCount", 0))
    except (TypeError, ValueError):
        previous_model_calls = 0
    # A cached compile is still backed by the model calls which produced its
    # plans.  Do not erase that provenance merely because this invocation did
    # not need to contact the model.
    runtime_model_calls = sum(1 for _ in (root / "runtime").glob("**/runtime_metadata.json"))
    compiled_model_calls = max(previous_model_calls, runtime_model_calls, model_calls)
    manifest = {
        "schemaVersion": COMPILE_VERSION,
        "status": "complete",
        "createdAt": utc_now(),
        "stage1RunId": contract.run_id,
        "stage1Draft": contract.is_draft,
        "contractFingerprint": contract.contract_fingerprint,
        "sourceFingerprint": contract.source_fingerprint,
        "compileFingerprint": fingerprints["aggregate"],
        "fingerprints": fingerprints,
        "model": {"provider": config.compiler.provider, "name": config.compiler.name},
        "transport": "openai_json",
        "modelCallCount": compiled_model_calls,
        "modelCallCountThisInvocation": model_calls,
        "plans": {
            endpoint: {
                **record,
                "fileSha256": file_sha256(Path(record["path"])),
            }
            for endpoint, record in sorted(results.items())
        },
    }
    atomic_write_json(manifest_path, manifest)
    return {
        "status": "complete",
        "compiledRoot": str(root),
        "manifest": str(manifest_path),
        "contractFingerprint": contract.contract_fingerprint,
        "modelCallCount": model_calls,
        "compiledModelCallCount": compiled_model_calls,
        "plans": results,
    }


def load_plans(
    config: Stage2Config,
    workspace: Path,
    *,
    stage1_bundle: Path | None = None,
    allow_draft: bool = False,
    root: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], Stage1Contract, Path]:
    contract = resolve_stage1_contract(
        config,
        workspace,
        stage1_bundle=stage1_bundle,
        allow_draft=allow_draft,
    )
    target = root or compiled_root(contract, config)
    manifest_path = target / "compile_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"compiled Stage 2 manifest is missing: {manifest_path}")
    manifest = read_json_object(manifest_path)
    expected_fingerprints = compile_fingerprints(config, contract)
    if manifest.get("compileFingerprint") != expected_fingerprints["aggregate"]:
        raise ValueError("compiled Stage 2 plan cache is incompatible with current prompt, schema, runtime, model, code, or Stage 1 contract")
    plans: dict[str, dict[str, Any]] = {}
    for endpoint, slug in ENDPOINT_SLUGS.items():
        path = target / f"{slug}.plan.json"
        if not path.is_file():
            raise FileNotFoundError(f"compiled Stage 2 plan is missing: {path}")
        plan = read_json_object(path)
        errors = validate_plan(plan, contract, endpoint)
        if errors:
            raise ValueError(f"compiled {slug} plan failed revalidation: {'; '.join(errors)}")
        plans[endpoint] = plan
    return plans, contract, target


__all__ = [
    "ALLOWED_OPS",
    "ENDPOINT_SLUGS",
    "PLAN_VERSION",
    "build_seed_plan",
    "compile_fingerprints",
    "compile_plan",
    "compiled_root",
    "endpoint_profile",
    "load_plans",
    "stage2_root",
    "validate_plan",
]
