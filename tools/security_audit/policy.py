from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import PreflightIssue, RuntimePolicy

from .checker.base import CheckerType
from .checker.registry import CheckerRegistry
from .config import CheckerConfig


_SELECTION_MODES = {"explicit", "recommend", "default"}

_RULE_BASED_CHECKERS = [
    "PIIRule",
    "SecretRule",
    "ToxicityKeywordRule",
    "HarmfulKeywordRule",
    "BiasKeywordRule",
]

_LLM_JUDGE_CHECKERS = [
    "HarmfulContentLLMJudge",
    "BiasLLMJudge",
    "ToxicityLLMJudge",
    "PIILLMJudge",
    "SycophancyLLMJudge",
    "PromptInjectionLLMJudge",
    "JailbreakLLMJudge",
    "FactualInconsistancyLLMJudge",
    "SelfContradictionLLMJudge",
    "InstructionMismatchLLMJudge",
    "DPOLabelFlipLLMJudge",
]

_STANDARD_MODEL_CHECKERS = [
    "PIINERDetector",
]

_HEAVY_MODEL_CHECKERS = [
    "HarmfulContentClassifier",
    "ToxicityClassifier",
    "BiasClassifier",
    "JailbreakClassifier",
    "PromptInjectionClassifier",
    "GraCeFulBackdoorDefender",
]

_HEAVY_CHECKERS = set(_HEAVY_MODEL_CHECKERS)

_RESOURCE_TIER_ORDER = {"light": 0, "standard": 1, "full": 2}

_CHECKER_MIN_RESOURCE_TIERS = {
    **{name: "light" for name in _RULE_BASED_CHECKERS},
    **{name: "standard" for name in _LLM_JUDGE_CHECKERS},
    **{name: "standard" for name in _STANDARD_MODEL_CHECKERS},
    **{name: "full" for name in _HEAVY_MODEL_CHECKERS},
}

_LOCAL_MODEL_PATH_CHECKERS = {
    "HarmfulContentClassifier",
    "JailbreakClassifier",
    "PromptInjectionClassifier",
    "BiasClassifier",
}

_SPACY_MODEL_BY_LANGUAGE = {
    "en": "en_core_web_lg",
}

_TOKENIZER_FILES = {
    "tokenizer.json",
    "tokenizer.model",
    "vocab.json",
    "vocab.txt",
    "merges.txt",
    "spiece.model",
}

_WEIGHT_SUFFIXES = (".safetensors", ".bin")


@dataclass(frozen=True)
class CheckerRequest:
    selection_mode: str = "default"
    checker_names: list[str] = field(default_factory=list)
    checker_preferences: str = ""
    strategy: str = "single_stage"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def explicit(self) -> bool:
        return self.selection_mode == "explicit"


@dataclass
class CheckerCapability:
    name: str
    allowed: bool
    checker_type: str | None = None
    risk_type: str | None = None
    required_tier: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    issues: list[PreflightIssue] = field(default_factory=list)

    @property
    def blocked_reason(self) -> str | None:
        if self.allowed or not self.issues:
            return None
        return self.issues[0].code


@dataclass
class CheckerCapabilitySet:
    capabilities: dict[str, CheckerCapability]

    def get(self, checker_name: str) -> CheckerCapability | None:
        return self.capabilities.get(checker_name)

    def is_allowed(self, checker_name: str) -> bool:
        capability = self.get(checker_name)
        return bool(capability and capability.allowed)

    def allowed_names(self) -> list[str]:
        return sorted(name for name, capability in self.capabilities.items() if capability.allowed)

    def blocked(self) -> list[CheckerCapability]:
        return sorted(
            [capability for capability in self.capabilities.values() if not capability.allowed],
            key=lambda capability: capability.name,
        )

    def blocked_metadata(self) -> list[dict[str, Any]]:
        blocked = []
        for capability in self.blocked():
            blocked.append({
                "name": capability.name,
                "reasons": [issue.code for issue in capability.issues] or ["checker_not_allowed"],
            })
        return blocked


@dataclass
class ResolvedCheckerPlan:
    strategy: str
    source: str
    checker_configs: list[CheckerConfig]
    skipped_checkers: list[dict[str, str]] = field(default_factory=list)
    degradations: list[dict[str, str]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    request: CheckerRequest | None = None
    capability_set: CheckerCapabilitySet | None = None


def build_checker_request(kwargs: dict, tool_defaults: dict) -> CheckerRequest:
    # TODO: (resource_tier) Let security_audit strategies accept explicit
    # strategy names once funnel policies are implemented.
    strategy = str(kwargs.get("strategy") or tool_defaults.get("strategy") or "single_stage")
    raw = dict(kwargs)
    raw_checker_names = kwargs.get("checker_names")
    has_checker_names = "checker_names" in kwargs
    raw_mode = kwargs.get("checker_selection_mode")
    selection_mode = str(raw_mode or "").strip().lower()

    if not selection_mode:
        selection_mode = "explicit" if has_checker_names else "default"

    checker_names = raw_checker_names if isinstance(raw_checker_names, list) else []
    raw_checker_preferences = kwargs.get("checker_preferences")
    checker_preferences = raw_checker_preferences if isinstance(raw_checker_preferences, str) else ""
    if selection_mode == "default":
        checker_preferences = ""

    return CheckerRequest(
        selection_mode=selection_mode,
        checker_names=_unique_checker_names(checker_names),
        checker_preferences=checker_preferences,
        strategy=strategy,
        raw=raw,
    )


def _unique_checker_names(raw_names: list[Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def validate_checker_request(
    request: CheckerRequest,
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    if request.selection_mode not in _SELECTION_MODES:
        issues.append(PreflightIssue(
            level="error",
            code="invalid_checker_selection_mode",
            message=(
                "checker_selection_mode must be one of "
                f"{sorted(_SELECTION_MODES)}, got {request.selection_mode!r}."
            ),
        ))

    if not isinstance(request.strategy, str) or not request.strategy.strip():
        issues.append(PreflightIssue(
            level="error",
            code="invalid_security_audit_strategy",
            message="security_audit strategy must be a non-empty string.",
        ))

    raw_checker_names = request.raw.get("checker_names")
    has_checker_names = "checker_names" in request.raw

    if has_checker_names and not isinstance(raw_checker_names, list):
        issues.append(PreflightIssue(
            level="error",
            code="invalid_checker_names",
            message="checker_names must be a list of checker class names.",
        ))

    if (
        request.selection_mode != "default"
        and "checker_preferences" in request.raw
        and not isinstance(request.raw.get("checker_preferences"), str)
    ):
        issues.append(PreflightIssue(
            level="error",
            code="invalid_checker_preferences",
            message="checker_preferences must be a string.",
        ))

    if request.selection_mode != "explicit":
        return issues

    if not request.checker_names:
        return issues + [PreflightIssue(
            level="error",
            code="empty_checker_names",
            message="explicit checker selection requires a non-empty checker_names list.",
        )]

    available = _available_checker_names()
    for raw_name in raw_checker_names if isinstance(raw_checker_names, list) else []:
        if not isinstance(raw_name, str) or not raw_name.strip():
            issues.append(PreflightIssue(
                level="error",
                code="invalid_checker_name",
                message=f"checker_names entries must be non-empty strings, got {raw_name!r}.",
            ))
            continue
        name = raw_name.strip()
        if name not in available:
            issues.append(PreflightIssue(
                level="error",
                code="unknown_checker",
                checker_name=name,
                message=f"Checker `{name}` is not registered.",
            ))
            continue
        resource_issue = _validate_checker_resource_tier(name, runtime_policy)
        if resource_issue:
            issues.append(resource_issue)
        if runtime_policy.offline and name.endswith("LLMJudge") and not _has_local_llm_config(context_config):
            issues.append(_offline_missing_llm_issue(name))
    return issues


def build_checker_capability_set(
    *,
    tool_defaults: dict,
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> CheckerCapabilitySet:
    default_configs = load_default_checker_configs(tool_defaults)
    params_by_name = {config.name: dict(config.params) for config in default_configs}
    capabilities: dict[str, CheckerCapability] = {}

    for name in sorted(_available_checker_names()):
        issues: list[PreflightIssue] = []
        resource_issue = _validate_checker_resource_tier(name, runtime_policy)
        if resource_issue:
            issues.append(resource_issue)
        else:
            # Resource tier is checked first because it is cheap. Network/offline
            # checks only run for resource-eligible checkers.
            issues.extend(validate_checker_network_availability(
                checker_config=CheckerConfig(
                    name=name,
                    params=dict(params_by_name.get(name, {})),
                ),
                runtime_policy=runtime_policy,
                context_config=context_config,
            ))

        capabilities[name] = CheckerCapability(
            name=name,
            checker_type=_checker_type(name),
            risk_type=_risk_type(name),
            allowed=not any(issue.level == "error" for issue in issues),
            required_tier=_CHECKER_MIN_RESOURCE_TIERS.get(name),
            params=dict(params_by_name.get(name, {})),
            issues=issues,
        )
    return CheckerCapabilitySet(capabilities=capabilities)


def resolve_checker_plan(
    *,
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    tool_defaults: dict,
    resource_tier: str,
) -> ResolvedCheckerPlan:
    default_configs = load_default_checker_configs(tool_defaults)
    defaults_by_name = {config.name: config for config in default_configs}

    if request.selection_mode == "explicit":
        checker_configs, skipped = _checker_configs_from_names(
            names=request.checker_names,
            defaults_by_name=defaults_by_name,
            capability_set=capability_set,
        )
        return _resolved_plan(
            request=request,
            capability_set=capability_set,
            strategy="deterministic",
            checker_configs=checker_configs,
            skipped_checkers=skipped,
            source="explicit",
        )

    if request.selection_mode == "recommend":
        names, degradations = _recommend_checker_names(
            request=request,
            capability_set=capability_set,
            default_configs=default_configs,
            resource_tier=resource_tier,
        )
        checker_configs, skipped = _checker_configs_from_names(
            names=names,
            defaults_by_name=defaults_by_name,
            capability_set=capability_set,
        )
        return _resolved_plan(
            request=request,
            capability_set=capability_set,
            strategy="deterministic",
            checker_configs=checker_configs,
            skipped_checkers=skipped,
            degradations=degradations,
            source="recommend",
        )

    if default_configs:
        checker_configs: list[CheckerConfig] = []
        skipped: list[dict[str, str]] = []
        for config in default_configs:
            if not config.enabled:
                continue
            if capability_set.is_allowed(config.name):
                checker_configs.append(config.copy(deep=True))
                continue
            capability = capability_set.get(config.name)
            skipped.append({
                "name": config.name,
                "reason": capability.blocked_reason if capability else "unknown_checker",
            })
        return _resolved_plan(
            request=request,
            capability_set=capability_set,
            strategy="deterministic",
            checker_configs=checker_configs,
            skipped_checkers=skipped,
            source="default",
        )

    checker_configs = []
    skipped = []
    for name in resolve_default_checkers_for_resource_tier(resource_tier):
        if capability_set.is_allowed(name):
            checker_configs.append(CheckerConfig(name=name))
        else:
            capability = capability_set.get(name)
            skipped.append({
                "name": name,
                "reason": capability.blocked_reason if capability else "unknown_checker",
            })
    return _resolved_plan(
        request=request,
        capability_set=capability_set,
        strategy="deterministic",
        checker_configs=checker_configs,
        skipped_checkers=skipped,
        source="fallback",
    )


def validate_resolved_checker_plan(
    *,
    plan: ResolvedCheckerPlan,
    capability_set: CheckerCapabilitySet,
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    enabled_configs = [config for config in plan.checker_configs if config.enabled]
    if not enabled_configs:
        issues.append(PreflightIssue(
            level="error",
            code="no_enabled_checkers",
            message="security_audit resolved plan has no enabled checkers.",
        ))
        return issues

    enabled_names = {config.name for config in enabled_configs}
    stage_names: set[str] = set()
    for stage in plan.stages:
        stage_checkers = stage.get("checkers", [])
        if not isinstance(stage_checkers, list):
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_checkers",
                message=f"Resolved stage `{stage.get('id', '')}` checkers must be a list.",
            ))
            continue

        for checker_name in stage_checkers:
            if not isinstance(checker_name, str) or not checker_name.strip():
                issues.append(PreflightIssue(
                    level="error",
                    code="invalid_stage_checker_name",
                    message=(
                        f"Resolved stage `{stage.get('id', '')}` checker names must be "
                        f"non-empty strings, got {checker_name!r}."
                    ),
                ))
                continue

            normalized_name = checker_name.strip()
            stage_names.add(normalized_name)
            if not capability_set.is_allowed(normalized_name):
                issues.append(PreflightIssue(
                    level="error",
                    code="stage_checker_not_allowed",
                    checker_name=normalized_name,
                    message=(
                        f"Resolved stage `{stage.get('id', '')}` contains checker "
                        f"`{checker_name}` outside the current capability set."
                    ),
                ))
            if normalized_name not in enabled_names:
                issues.append(PreflightIssue(
                    level="error",
                    code="stage_checker_not_enabled",
                    checker_name=normalized_name,
                    message=(
                        f"Resolved stage `{stage.get('id', '')}` contains checker "
                        f"`{checker_name}` that is not enabled in checker_configs."
                    ),
                ))

    for checker_config in enabled_configs:
        if checker_config.name not in stage_names:
            issues.append(PreflightIssue(
                level="error",
                code="enabled_checker_missing_from_stages",
                checker_name=checker_config.name,
                message=(
                    f"Enabled checker `{checker_config.name}` is missing from resolved plan stages."
                ),
            ))

    available = _available_checker_names()
    for checker_config in enabled_configs:
        name = checker_config.name
        if name not in available:
            issues.append(PreflightIssue(
                level="error",
                code="unknown_checker",
                checker_name=name,
                message=f"Checker `{name}` is not registered.",
            ))
            continue

        capability = capability_set.get(name)
        if capability is None or not capability.allowed:
            capability_issues = capability.issues if capability else []
            issues.extend(capability_issues or [PreflightIssue(
                level="error",
                code="checker_not_allowed",
                checker_name=name,
                message=f"Checker `{name}` is not allowed by the current deployment policy.",
            )])
            continue

        # TODO: (network_mode) Extend resolved-plan checks to cover checker
        # implementations that can still perform implicit downloads internally.
        if runtime_policy.offline and _requires_transformers_local_files_only(name):
            if checker_config.params.get("local_files_only") is not True:
                issues.append(PreflightIssue(
                    level="error",
                    code="offline_checker_missing_local_files_only",
                    checker_name=name,
                    message=(
                        f"Checker `{name}` must receive params.local_files_only=True "
                        "when deployment.network_mode=offline."
                    ),
                ))
    return issues


def checker_request_metadata(request: CheckerRequest) -> dict[str, Any]:
    return {
        "checker_selection_mode": request.selection_mode,
        "checker_names": list(request.checker_names),
        "checker_preferences": request.checker_preferences,
    }


def capability_set_metadata(capability_set: CheckerCapabilitySet) -> dict[str, Any]:
    return {
        "allowed_checker_names": capability_set.allowed_names(),
        "blocked_checkers": capability_set.blocked_metadata(),
    }


def resolved_plan_metadata(plan: ResolvedCheckerPlan) -> dict[str, Any]:
    return {
        "schema_version": "security_audit.resolved_plan.v1",
        "strategy": plan.strategy,
        "source": plan.source,
        "stages": list(plan.stages),
        "skipped_checkers": list(plan.skipped_checkers),
        "degradations": list(plan.degradations),
    }


def _resolved_plan(
    *,
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    strategy: str,
    checker_configs: list[CheckerConfig],
    skipped_checkers: list[dict[str, str]] | None = None,
    degradations: list[dict[str, str]] | None = None,
    source: str,
) -> ResolvedCheckerPlan:
    enabled_names = [config.name for config in checker_configs if config.enabled]
    return ResolvedCheckerPlan(
        strategy=strategy,
        checker_configs=checker_configs,
        skipped_checkers=skipped_checkers or [],
        degradations=degradations or [],
        source=source,
        stages=[_single_stage(enabled_names)],
        request=request,
        capability_set=capability_set,
    )


def _single_stage(checker_names: list[str]) -> dict[str, Any]:
    return {
        "id": "stage_1",
        "name": "single_stage",
        "type": "single_stage",
        "checkers": list(checker_names),
        "input_scope": {
            "type": "all_samples",
            "source_stage_id": None,
        },
        "routing": {
            "mode": "all",
        },
    }


def _checker_configs_from_names(
    *,
    names: list[str],
    defaults_by_name: dict[str, CheckerConfig],
    capability_set: CheckerCapabilitySet,
) -> tuple[list[CheckerConfig], list[dict[str, str]]]:
    checker_configs: list[CheckerConfig] = []
    skipped: list[dict[str, str]] = []

    for name in names:
        if not capability_set.is_allowed(name):
            capability = capability_set.get(name)
            skipped.append({
                "name": name,
                "reason": capability.blocked_reason if capability else "unknown_checker",
            })
            continue

        default_config = defaults_by_name.get(name)
        checker_configs.append(CheckerConfig(
            name=name,
            enabled=True,
            params=dict(default_config.params) if default_config else {},
        ))
    return checker_configs, skipped


def _recommend_checker_names(
    *,
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    default_configs: list[CheckerConfig],
    resource_tier: str,
) -> tuple[list[str], list[dict[str, str]]]:
    allowed = set(capability_set.allowed_names())
    preference = request.checker_preferences.lower()

    fast_markers = ("fast", "quick", "low cost", "low-cost", "cheap", "light", "成本", "快速", "低成本")
    broad_markers = ("accurate", "accuracy", "coverage", "strong", "full", "全面", "准确", "覆盖")

    if any(marker in preference for marker in fast_markers):
        names = [name for name in _RULE_BASED_CHECKERS if name in allowed]
        if names:
            return names, []

    if any(marker in preference for marker in broad_markers):
        names = [
            name for name in _coverage_checker_names_for_resource_tier(resource_tier)
            if name in allowed
        ]
        if names:
            return names, []

    configured = [config.name for config in default_configs if config.enabled and config.name in allowed]
    if configured:
        return configured, [{
            "reason": "recommendation_fell_back_to_default_config",
            "from": "recommend",
            "to": "default",
        }]

    fallback = [
        name for name in resolve_default_checkers_for_resource_tier(resource_tier)
        if name in allowed
    ]
    return fallback, [{
        "reason": "recommendation_fell_back_to_resource_tier_defaults",
        "from": "recommend",
        "to": "fallback",
    }]


def load_default_checker_configs(tool_defaults: dict) -> list[CheckerConfig]:
    raw = tool_defaults.get("checkers")
    if not isinstance(raw, list):
        return []

    configs: list[CheckerConfig] = []
    for item in raw:
        if isinstance(item, str):
            configs.append(CheckerConfig(name=item))
        elif isinstance(item, dict) and "name" in item:
            configs.append(CheckerConfig(**item))
    return configs


def _available_checker_names() -> set[str]:
    return set(CheckerRegistry.list_all())


def _checker_type(checker_name: str) -> str | None:
    try:
        checker_type = getattr(CheckerRegistry.get(checker_name), "checker_type", None)
    except KeyError:
        return None
    if isinstance(checker_type, CheckerType):
        return checker_type.value
    return str(checker_type) if checker_type else None


def _risk_type(checker_name: str) -> str | None:
    try:
        risk_type = getattr(CheckerRegistry.get(checker_name), "risk_type", None)
    except KeyError:
        return None
    value = getattr(risk_type, "value", None)
    return str(value or risk_type) if risk_type else None


def _validate_checker_resource_tier(
    checker_name: str,
    runtime_policy: RuntimePolicy,
) -> PreflightIssue | None:
    required_tier = _CHECKER_MIN_RESOURCE_TIERS.get(checker_name)
    if required_tier is None:
        return None

    current_tier = runtime_policy.resource_tier
    current_rank = _RESOURCE_TIER_ORDER.get(current_tier)
    required_rank = _RESOURCE_TIER_ORDER[required_tier]
    if current_rank is None or current_rank >= required_rank:
        return None

    return PreflightIssue(
        level="error",
        code="checker_resource_tier_too_low",
        checker_name=checker_name,
        message=(
            f"Checker `{checker_name}` requires deployment.resource_tier >= {required_tier!r}, "
            f"but current resource_tier is {current_tier!r}."
        ),
    )


def _requires_transformers_local_files_only(checker_name: str) -> bool:
    return checker_name in _LOCAL_MODEL_PATH_CHECKERS or checker_name == "GraCeFulBackdoorDefender"


def _offline_missing_llm_issue(checker_name: str) -> PreflightIssue:
    return PreflightIssue(
        level="error",
        code="offline_checker_missing_llm",
        checker_name=checker_name,
        message=(
            "LLM judge checkers require a configured local or intranet LLM "
            "endpoint when deployment.network_mode=offline."
        ),
    )


def resolve_default_checkers_for_resource_tier(resource_tier: str) -> list[str]:
    normalized = (resource_tier or "light").strip().lower()
    if normalized == "standard":
        return [
            *_RULE_BASED_CHECKERS,
            *_STANDARD_MODEL_CHECKERS,
            *_LLM_JUDGE_CHECKERS,
        ]
    if normalized == "full":
        return [
            *_RULE_BASED_CHECKERS,
            *_STANDARD_MODEL_CHECKERS,
            *_LLM_JUDGE_CHECKERS,
            *_HEAVY_MODEL_CHECKERS,
        ]
    return list(_RULE_BASED_CHECKERS)


def _coverage_checker_names_for_resource_tier(resource_tier: str) -> list[str]:
    names = resolve_default_checkers_for_resource_tier(resource_tier)
    if (resource_tier or "").strip().lower() == "full":
        available = _available_checker_names()
        for name in sorted(available):
            if name not in names:
                names.append(name)
    return names


def validate_checker_network_availability(
    *,
    checker_config: CheckerConfig,
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> list[PreflightIssue]:
    if not runtime_policy.offline:
        # TODO: (network_mode) Add best-effort dependency warnings for online
        # mode once checker dependency metadata is finalized.
        return []

    name = checker_config.name

    if name.endswith("LLMJudge") and not _has_local_llm_config(context_config):
        return [_offline_missing_llm_issue(name)]

    if name in _LOCAL_MODEL_PATH_CHECKERS:
        model_path = _resolve_model_path(checker_config)
        if not model_path:
            return [PreflightIssue(
                level="error",
                code="offline_checker_missing_model_path",
                checker_name=name,
                message=(
                    "Offline model-based checker requires a local model path. "
                    "Set checker params.model_name_or_path in "
                    "tools/security_audit/default.yaml."
                ),
            )]
        if _looks_like_hf_model_id(model_path):
            return [PreflightIssue(
                level="error",
                code="offline_checker_uses_hf_model_id",
                checker_name=name,
                message=(
                    f"Configured model_name_or_path={model_path!r} looks like a HuggingFace model id. "
                    "Offline mode requires a local model directory."
                ),
            )]
        if not _path_exists(model_path):
            return [PreflightIssue(
                level="error",
                code="offline_checker_model_path_not_found",
                checker_name=name,
                message=f"Configured local model path does not exist: {model_path!r}.",
            )]
        issue = _validate_transformers_model_dir(name, model_path)
        if issue:
            return [issue]

    if name == "ToxicityClassifier":
        return [PreflightIssue(
            level="error",
            code="offline_checker_not_local_only_ready",
            checker_name=name,
            message=(
                "ToxicityClassifier uses Detoxify, which may fetch weights from package caches "
                "or remote sources. It is disabled in offline mode until an explicit local "
                "weights/cache contract is implemented."
            ),
        )]

    if name == "PIINERDetector":
        issue = _validate_pii_ner_offline(checker_config)
        if issue:
            return [issue]

    if name == "GraCeFulBackdoorDefender":
        victim_config = checker_config.params.get("victim_config") or {}
        victim_path = victim_config.get("path") if isinstance(victim_config, dict) else None
        if not victim_path:
            return [PreflightIssue(
                level="error",
                code="offline_checker_missing_victim_model",
                checker_name=name,
                message=(
                    "GraCeFulBackdoorDefender requires params.victim_config.path "
                    "to point to a local victim/surrogate model in offline mode."
                ),
            )]
        if not _path_exists(victim_path):
            return [PreflightIssue(
                level="error",
                code="offline_checker_victim_model_not_found",
                checker_name=name,
                message=f"Configured victim model path does not exist: {victim_path!r}.",
            )]
        issue = _validate_transformers_model_dir(name, victim_path)
        if issue:
            return [issue]

    return []

def _has_local_llm_config(context_config: dict[str, Any]) -> bool:
    tool_llm = _get_section(context_config, "tool_llm")
    agent = _get_section(context_config, "agent")
    return bool(
        (_get_value(tool_llm, "model") and _get_value(tool_llm, "base_url"))
        or (_get_value(agent, "model") and _get_value(agent, "base_url"))
    )


def _resolve_model_path(checker_config: CheckerConfig) -> str | None:
    explicit = checker_config.params.get("model_name_or_path")
    return str(explicit) if explicit else None


def _path_exists(value: str) -> bool:
    return Path(value).expanduser().exists()


def _validate_transformers_model_dir(checker_name: str, model_path: str) -> PreflightIssue | None:
    path = Path(model_path).expanduser()
    if not path.is_dir():
        return PreflightIssue(
            level="error",
            code="offline_checker_model_path_not_directory",
            checker_name=checker_name,
            message=f"Configured model path must be a local directory: {model_path!r}.",
        )
    if not (path / "config.json").exists():
        return PreflightIssue(
            level="error",
            code="offline_checker_model_missing_config",
            checker_name=checker_name,
            message=f"Local model directory is missing config.json: {model_path!r}.",
        )
    if not any((path / filename).exists() for filename in _TOKENIZER_FILES):
        return PreflightIssue(
            level="error",
            code="offline_checker_model_missing_tokenizer",
            checker_name=checker_name,
            message=f"Local model directory is missing tokenizer files: {model_path!r}.",
        )
    if not any(file.is_file() and file.name.endswith(_WEIGHT_SUFFIXES) for file in path.rglob("*")):
        return PreflightIssue(
            level="error",
            code="offline_checker_model_missing_weights",
            checker_name=checker_name,
            message=f"Local model directory is missing .safetensors or .bin weights: {model_path!r}.",
        )
    return None


def _validate_pii_ner_offline(checker_config: CheckerConfig) -> PreflightIssue | None:
    if importlib.util.find_spec("spacy") is None:
        return PreflightIssue(
            level="error",
            code="offline_checker_missing_dependency",
            checker_name=checker_config.name,
            message="PIINERDetector requires spaCy to be installed in offline mode.",
        )
    if importlib.util.find_spec("presidio_analyzer") is None:
        return PreflightIssue(
            level="error",
            code="offline_checker_missing_dependency",
            checker_name=checker_config.name,
            message="PIINERDetector requires presidio-analyzer to be installed in offline mode.",
        )
    if importlib.util.find_spec("presidio_anonymizer") is None:
        return PreflightIssue(
            level="error",
            code="offline_checker_missing_dependency",
            checker_name=checker_config.name,
            message="PIINERDetector requires presidio-anonymizer to be installed in offline mode.",
        )

    import spacy

    language = str(checker_config.params.get("language", "en"))
    spacy_model = _SPACY_MODEL_BY_LANGUAGE.get(language, f"{language}_core_web_sm")
    if not spacy.util.is_package(spacy_model):
        return PreflightIssue(
            level="error",
            code="offline_checker_missing_spacy_model",
            checker_name=checker_config.name,
            message=(
                f"PIINERDetector requires spaCy model {spacy_model!r} to be installed. "
                "Offline mode will not run spacy.cli.download()."
            ),
        )
    return None


def _looks_like_hf_model_id(value: str) -> bool:
    return "/" in value and not value.startswith(("/", "./", "../", "~"))


def _get_section(config: dict[str, Any], name: str) -> Any:
    section = config.get(name, {}) if isinstance(config, dict) else {}
    return section


def _get_value(section: Any, name: str) -> Any:
    if isinstance(section, dict):
        return section.get(name)
    return getattr(section, name, None)
