from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import PreflightIssue, RuntimePolicy

from .checker.registry import CheckerRegistry
from .config import CheckerConfig


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
    raw_checker_names: Any = None
    has_checker_names: bool = False
    strategy: str = "single_stage"

    @property
    def explicit(self) -> bool:
        return self.has_checker_names

    @property
    def checker_names(self) -> list[str]:
        if isinstance(self.raw_checker_names, list):
            return [name for name in self.raw_checker_names if isinstance(name, str)]
        return []


@dataclass
class CheckerCapability:
    name: str
    allowed: bool
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


@dataclass
class ResolvedCheckerPlan:
    strategy: str
    checker_configs: list[CheckerConfig]
    skipped_checkers: list[dict[str, str]] = field(default_factory=list)
    source: str = "auto"


def build_checker_request(kwargs: dict, tool_defaults: dict) -> CheckerRequest:
    # TODO: (resource_tier) Let security_audit strategies accept explicit
    # strategy names once funnel policies are implemented.
    strategy = str(kwargs.get("strategy") or tool_defaults.get("strategy") or "single_stage")
    return CheckerRequest(
        raw_checker_names=kwargs.get("checker_names"),
        has_checker_names="checker_names" in kwargs,
        strategy=strategy,
    )


def validate_checker_request(
    request: CheckerRequest,
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    if not isinstance(request.strategy, str) or not request.strategy.strip():
        issues.append(PreflightIssue(
            level="error",
            code="invalid_security_audit_strategy",
            message="security_audit strategy must be a non-empty string.",
        ))

    if not request.has_checker_names:
        return issues

    if not isinstance(request.raw_checker_names, list):
        return issues + [PreflightIssue(
            level="error",
            code="invalid_checker_names",
            message="checker_names must be a list of checker class names.",
        )]

    if not request.raw_checker_names:
        return issues + [PreflightIssue(
            level="error",
            code="empty_checker_names",
            message="checker_names cannot be empty when explicitly provided.",
        )]

    available = _available_checker_names()
    for raw_name in request.raw_checker_names:
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
                    selection_source="capability",
                ),
                runtime_policy=runtime_policy,
                context_config=context_config,
            ))

        capabilities[name] = CheckerCapability(
            name=name,
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

    if request.explicit:
        checker_configs = []
        for name in request.checker_names:
            default_config = defaults_by_name.get(name)
            checker_configs.append(CheckerConfig(
                name=name,
                enabled=True,
                params=dict(default_config.params) if default_config else {},
                selection_source="explicit",
            ))
        return ResolvedCheckerPlan(
            strategy=request.strategy,
            checker_configs=checker_configs,
            source="explicit",
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
        return ResolvedCheckerPlan(
            strategy=request.strategy,
            checker_configs=checker_configs,
            skipped_checkers=skipped,
            source="config",
        )

    checker_configs = []
    skipped = []
    for name in resolve_default_checkers_for_resource_tier(resource_tier):
        if capability_set.is_allowed(name):
            checker_configs.append(CheckerConfig(name=name, selection_source="auto"))
        else:
            capability = capability_set.get(name)
            skipped.append({
                "name": name,
                "reason": capability.blocked_reason if capability else "unknown_checker",
            })
    return ResolvedCheckerPlan(
        strategy=request.strategy,
        checker_configs=checker_configs,
        skipped_checkers=skipped,
        source="auto",
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


def load_default_checker_configs(tool_defaults: dict) -> list[CheckerConfig]:
    raw = tool_defaults.get("checkers")
    if not isinstance(raw, list):
        return []

    configs: list[CheckerConfig] = []
    for item in raw:
        if isinstance(item, str):
            configs.append(CheckerConfig(name=item, selection_source="config"))
        elif isinstance(item, dict) and "name" in item:
            configs.append(CheckerConfig(**{**item, "selection_source": "config"}))
    return configs


def _available_checker_names() -> set[str]:
    return set(CheckerRegistry.list_all())


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
            *_STANDARD_MODEL_CHECKERS,
            *_LLM_JUDGE_CHECKERS,
        ]
    if normalized == "full":
        return [
            *_STANDARD_MODEL_CHECKERS,
            *_LLM_JUDGE_CHECKERS,
            *_HEAVY_MODEL_CHECKERS,
        ]
    return list(_RULE_BASED_CHECKERS)


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
