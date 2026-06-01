from __future__ import annotations

import importlib.util
import json
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

_RESOURCE_TIER_ORDER = {"light": 0, "standard": 1, "full": 2}

_RESOLVE_STRATEGIES = {"deterministic", "llm"}

_ROUTING_MODES = {"all_samples", "field_applicable", "uncertain", "sample"}
_ROUTING_RULES = {
    "near_threshold",
    "conflicting",
    "low_confidence",
    "error",
    "content_filter",
    "unflagged",
}
_ROUTING_FIELDS = {
    "messages",
    "response",
    "chosen_response",
    "rejected_response",
    "context",
    "ground_truth_answer",
    "reference_answer",
}
_ROUTING_DATASET_TYPES = {"sft", "rl", "dpo", "benchmark"}

_ROUTING_KEYS = {
    "mode",
    "source_stage_id",
    "rules",
    "rule",
    "dataset_types",
    "dataset_type",
    "required_fields",
    "fields",
    "sample_rate",
    "rate",
    "sample_size",
    "max_samples",
    "limit",
    "threshold",
    "threshold_margin",
    "margin",
}

_PROGRESSIVE_WORKFLOW_STAGE_CHECKERS = [
    {
        "id": "stage_1",
        "name": "quick_surface_scan",
        "type": "quick_scan",
        "risk_focus": ["pii", "secret", "harmful", "toxicity", "bias"],
        "checkers": [
            "PIIRule",
            "SecretRule",
            "HarmfulKeywordRule",
            "ToxicityKeywordRule",
            "BiasKeywordRule",
            "PIINERDetector",
        ],
        "routing": {"mode": "all_samples"},
        "purpose": "Optional low-cost pre-screen for fast or budget-constrained audits; skip this stage when the user prioritizes high precision and high recall over cost.",
    },
    {
        "id": "stage_2",
        "name": "semantic_safety_scan",
        "type": "semantic_scan",
        "risk_focus": [
            "pii",
            "harmful",
            "toxicity",
            "bias",
            "prompt_injection",
            "jailbreak",
        ],
        "checkers": [
            "PIILLMJudge",
            "HarmfulContentLLMJudge",
            "ToxicityLLMJudge",
            "BiasLLMJudge",
            "PromptInjectionLLMJudge",
            "JailbreakLLMJudge",
        ],
        "routing": {"mode": "all_samples"},
        "purpose": "Review semantic safety and alignment-bypass risks on samples with auditable text.",
    },
    {
        "id": "stage_3",
        "name": "instruction_following_scan",
        "type": "semantic_scan",
        "risk_focus": ["instruction_mismatch"],
        "checkers": ["InstructionMismatchLLMJudge"],
        "routing": {
            "mode": "field_applicable",
        },
        "purpose": "Audit whether responses follow instructions only when both instructions/messages and a response are present.",
    },
    {
        "id": "stage_4",
        "name": "self_contradiction_scan",
        "type": "semantic_scan",
        "risk_focus": ["self_contradiction"],
        "checkers": ["SelfContradictionLLMJudge"],
        "routing": {
            "mode": "field_applicable",
        },
        "purpose": "Audit internal response contradictions only on samples that contain a model response or assistant message.",
    },
    {
        "id": "stage_5",
        "name": "sycophancy_scan",
        "type": "semantic_scan",
        "risk_focus": ["sycophancy"],
        "checkers": ["SycophancyLLMJudge"],
        "routing": {
            "mode": "field_applicable",
        },
        "purpose": "Audit sycophancy only when user messages and a model response are available.",
    },
    {
        "id": "stage_6",
        "name": "dpo_pairwise_scan",
        "type": "semantic_scan",
        "risk_focus": ["label_flipping"],
        "checkers": ["DPOLabelFlipLLMJudge"],
        "routing": {
            "mode": "field_applicable",
            "dataset_types": ["dpo"],
        },
        "purpose": "Audit DPO preference pairs independently from general semantic stages because the checker requires chosen and rejected responses.",
    },
    {
        "id": "stage_7",
        "name": "factual_consistency_scan",
        "type": "semantic_scan",
        "risk_focus": ["factual_inconsistency"],
        "checkers": ["FactualInconsistancyLLMJudge"],
        "routing": {
            "mode": "field_applicable",
        },
        "purpose": "Audit factual consistency only when trusted context and a response are present.",
    },
    {
        "id": "stage_8",
        "name": "backdoor_scan",
        "type": "deep_scan",
        "risk_focus": [
            "backdoor"
        ],
        "checkers": [
            "GraCeFulBackdoorDefender"
        ],
        "routing": {
            "mode": "all_samples",
        },
        "purpose": "Scan backdoors.",
    },
    {
        "id": "stage_9",
        "name": "risk_review",
        "type": "deep_scan",
        "risk_focus": [
            "jailbreak",
            "prompt_injection",
            "harmful",
            "toxicity",
            "bias",
        ],
        "checkers": [
            "HarmfulContentClassifier",
            "ToxicityClassifier",
            "BiasClassifier",
            "JailbreakClassifier",
            "PromptInjectionClassifier",
        ],
        "routing": {
            "mode": "uncertain",
            "source_stage_id": "stage_2",
            "rules": ["error", "content_filter", "near_threshold", "low_confidence"],
        },
        "purpose": "Review samples flagged for potential risks.",
    }
]

LLM_RESOLVER_OUTPUT_SCHEMA: dict[str, Any] = {
    "objective": {
        "goal": "string",
        "covered_risk_types": ["string"],
        "optimization": ["low_cost|fast|high_recall|high_precision|coverage|compliance"],
    },
    "stages": [
        {
            "id": "stage_number",
            "name": "string",
            "type": "quick_scan|semantic_scan|deep_scan",
            "risk_focus": ["risk_type_name"],
            "checkers": ["CheckerClassName"],
        "routing": {
            "mode": "all_samples|field_applicable|uncertain|sample",
            "source_stage_id": "string|null",
            "rules": ["near_threshold|conflicting|low_confidence|error|content_filter|unflagged"],
            "dataset_types": ["sft|rl|dpo|benchmark"],
            "sample_rate": "optional number",
            "sample_size": "optional number",
            "threshold": "optional number",
            "threshold_margin": "optional number",
            },
            "purpose": "string",
        }
    ],
}

LLM_RESOLVER_PROMPT_TEMPLATE = """You are the SecurityAudit checker-plan resolver.
Resolve the user's audit intent into an executable JSON plan.

Hard constraints:
- Select checkers only from allowed_checker_names.
- Never invent checker names.
- The local resolver owns schema_version, strategy, and source; do not output them.
- Do not force a fixed three-stage plan. The stage list is a flexible execution plan; use as many or as few stages as the audit intent requires.
- Prefer a non-empty risk_focus list for every stage. Put risk names in stage.risk_focus.
- Prefer not to reuse the same checker in multiple stages. Later stages should usually use stronger or more specialized checkers rather than rerunning earlier cheap/checker stages.
- A review stage must use checker(s) for the same risk type that have not already appeared earlier in the plan, and those checker(s) should be stronger or more specialized than the upstream checker(s). If no unused stronger/specialized checker is available for that risk type, skip the review stage instead of rerunning earlier checker(s).
- Build funnel-style plans only when they match the user intent. For fast or low-cost audits, cheap rule-based checks can run first. For high-precision and high-recall audits, skip low-precision/low-recall rule-based quick_scan stages and start from stronger LLM judges or model/guard checkers. Later stages should use stronger or more specialized checkers, not repeat earlier checkers.
- A stage should group checkers that share similar cost, data applicability, and routing. Split checkers into separate stages when they need different dataset types, required fields, upstream source stages, sampling, or review rules.
- Treat the Progressive Risk Audit Workflow as a menu of reusable stage patterns, not a required sequence. You may remove, reorder, merge, or split patterns as long as dependencies are valid. In particular, quick_scan is optional and should be omitted when it does not improve the requested precision/recall objective.
- Field-specific checks should usually be independent stages with explicit routing. For routing.mode="field_applicable", the local resolver reads each selected checker's required_fields from its checker card and filters samples per checker; do not output routing.required_fields or infer field names yourself.
- For partial-risk requests, remove irrelevant stages or checkers from the workflow.
- If a requested target cannot be satisfied by the allowed capability set, omit unavailable checkers from stages; the local resolver will derive skipped_checkers and degradations deterministically.
- Prefer low-cost routing, sampling, and rule-based checkers only when the user asks for fast, low-cost, preview, or very large-scale audits. Do not include rule-based checkers by default for high-precision high-recall audits unless they are explicitly requested or provide unique coverage such as secrets.

Routing guidance:
- routing may only contain these keys: mode, source_stage_id, rules, dataset_types, sample_rate, sample_size, threshold, threshold_margin.
- Do not put risk_types or checker_names inside routing; use stage.risk_focus and stage.checkers instead.
- Use mode=all_samples only for checks that should run over every sample. Rule-based all-sample quick scans are mainly for fast/low-cost pre-screening, not mandatory for high-precision high-recall plans.
- Use mode=field_applicable for checks that require specific real DataSample fields or dataset types; add dataset_types when known, but leave field requirements to the local checker-card logic.
- Use mode=sample for low-cost previews over applicable samples, or for expensive specialist checks when cost control is requested; set sample_rate or sample_size only with mode=sample.
- Use mode=uncertain only for a later review/recall stage with unused stronger/specialized checker(s) for the same risk type as the source stage. For high-precision review, use rules such as ["near_threshold", "conflicting", "low_confidence"], usually with threshold=0.5 and threshold_margin=0.1. For high-recall second-pass scanning, use rules=["unflagged"] to route samples not flagged by the source stage to a stronger checker. If all suitable checkers for that risk type have already been used or are unavailable, omit the uncertain review stage.
- source_stage_id must reference an earlier stage id in your output.
- Omit optional routing keys when they are not needed; do not output null values for sample_rate, sample_size, threshold, or threshold_margin.
- Do not mix unflagged with near_threshold/low_confidence/conflicting unless the user explicitly asks for both high recall and boundary-case review.

- Return JSON only. Do not wrap it in Markdown.

Output schema:
{output_schema}

Resolver input:
{resolver_input}
"""


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
    audit_intent: str = ""
    strategy: str = "llm"
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
    objective: dict[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    request: CheckerRequest | None = None
    capability_set: CheckerCapabilitySet | None = None


def build_checker_request(kwargs: dict, tool_defaults: dict) -> CheckerRequest:
    strategy = str(kwargs.get("strategy") or tool_defaults.get("strategy") or "llm")
    raw = dict(kwargs)
    raw_checker_names = kwargs.get("checker_names")
    has_checker_names = "checker_names" in kwargs
    raw_mode = kwargs.get("checker_selection_mode")
    selection_mode = str(raw_mode or "").strip().lower()

    if not selection_mode:
        selection_mode = "explicit" if has_checker_names else "default"

    checker_names = raw_checker_names if isinstance(raw_checker_names, list) else []
    raw_audit_intent = kwargs.get("audit_intent")
    audit_intent = raw_audit_intent if isinstance(raw_audit_intent, str) else ""
    if selection_mode == "default":
        audit_intent = ""

    return CheckerRequest(
        selection_mode=selection_mode,
        checker_names=_unique_checker_names(checker_names),
        audit_intent=audit_intent,
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

    if request.strategy not in _RESOLVE_STRATEGIES:
        issues.append(PreflightIssue(
            level="error",
            code="invalid_security_audit_strategy",
            message=(
                "security_audit strategy must be one of "
                f"{sorted(_RESOLVE_STRATEGIES)}, got {request.strategy!r}."
            ),
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
        and "audit_intent" in request.raw
        and not isinstance(request.raw.get("audit_intent"), str)
    ):
        issues.append(PreflightIssue(
            level="error",
            code="invalid_audit_intent",
            message="audit_intent must be a string.",
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
    llm: Any | None = None,
    llm_model: str = "",
    logger: Any | None = None,
    data_profile: dict[str, Any] | None = None,
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

    if request.selection_mode == "recommend" and request.strategy == "llm":
        return _resolve_llm_checker_plan(
            request=request,
            capability_set=capability_set,
            defaults_by_name=defaults_by_name,
            tool_defaults=tool_defaults,
            resource_tier=resource_tier,
            source=request.selection_mode,
            llm=llm,
            llm_model=llm_model,
            logger=logger,
            data_profile=data_profile,
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
    seen_stage_ids: set[str] = set()
    for stage in plan.stages:
        stage_id = str(stage.get("id") or "")
        if not stage_id.strip():
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_id",
                message="Resolved stages must have a non-empty id.",
            ))
        elif stage_id in seen_stage_ids:
            issues.append(PreflightIssue(
                level="error",
                code="duplicate_stage_id",
                message=f"Resolved stage id `{stage_id}` is duplicated.",
            ))

        issues.extend(_validate_stage_routing(stage=stage, prior_stage_ids=seen_stage_ids))
        if stage_id.strip():
            seen_stage_ids.add(stage_id)

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


def _validate_stage_routing(
    *,
    stage: dict[str, Any],
    prior_stage_ids: set[str],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    stage_id = str(stage.get("id") or "")
    routing = stage.get("routing") if isinstance(stage.get("routing"), dict) else {}
    for key in routing:
        if key not in _ROUTING_KEYS:
            issues.append(PreflightIssue(
                level="error",
                code="unsupported_stage_routing_key",
                message=(
                    f"Resolved stage {stage_id!r} routing contains unsupported key {key!r}. "
                    f"Allowed keys: {sorted(_ROUTING_KEYS)}."
                ),
            ))

    mode = str(routing.get("mode") or "all_samples").strip()

    if mode not in _ROUTING_MODES:
        issues.append(PreflightIssue(
            level="error",
            code="invalid_stage_routing_mode",
            message=(
                f"Resolved stage `{stage_id}` has unsupported routing.mode `{mode}`. "
                f"Allowed modes: {sorted(_ROUTING_MODES)}."
            ),
        ))

    source_stage_id = routing.get("source_stage_id")
    if mode == "uncertain":
        if not source_stage_id:
            issues.append(PreflightIssue(
                level="error",
                code="missing_stage_routing_source",
                message=f"Resolved stage `{stage_id}` uses routing.mode='uncertain' but has no source_stage_id.",
            ))
        elif str(source_stage_id) not in prior_stage_ids:
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_routing_source",
                message=(
                    f"Resolved stage `{stage_id}` references source_stage_id `{source_stage_id}`, "
                    "which is not an earlier stage id."
                ),
            ))
    elif source_stage_id and str(source_stage_id) not in prior_stage_ids:
        issues.append(PreflightIssue(
            level="error",
            code="invalid_stage_routing_source",
            message=(
                f"Resolved stage `{stage_id}` references source_stage_id `{source_stage_id}`, "
                "which is not an earlier stage id."
            ),
        ))

    for field in _routing_string_values(routing.get("required_fields") or routing.get("fields")):
        if field not in _ROUTING_FIELDS:
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_routing_field",
                message=(
                    f"Resolved stage `{stage_id}` has unsupported required field `{field}`. "
                    f"Allowed fields: {sorted(_ROUTING_FIELDS)}."
                ),
            ))

    for dataset_type in _routing_string_values(routing.get("dataset_types") or routing.get("dataset_type")):
        if dataset_type not in _ROUTING_DATASET_TYPES:
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_routing_dataset_type",
                message=(
                    f"Resolved stage `{stage_id}` has unsupported dataset type `{dataset_type}`. "
                    f"Allowed dataset types: {sorted(_ROUTING_DATASET_TYPES)}."
                ),
            ))

    for rule in _routing_string_values(routing.get("rules") or routing.get("rule")):
        if rule not in _ROUTING_RULES:
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_routing_rule",
                message=(
                    f"Resolved stage `{stage_id}` has unsupported routing rule `{rule}`. "
                    f"Allowed rules: {sorted(_ROUTING_RULES)}."
                ),
            ))

    issues.extend(_validate_stage_routing_number(
        stage_id=stage_id,
        routing=routing,
        keys=("sample_rate", "rate"),
        minimum=0.0,
        maximum=1.0,
        integer=False,
    ))
    issues.extend(_validate_stage_routing_number(
        stage_id=stage_id,
        routing=routing,
        keys=("sample_size", "max_samples", "limit"),
        minimum=0.0,
        maximum=None,
        integer=True,
    ))
    issues.extend(_validate_stage_routing_number(
        stage_id=stage_id,
        routing=routing,
        keys=("threshold",),
        minimum=0.0,
        maximum=1.0,
        integer=False,
    ))
    issues.extend(_validate_stage_routing_number(
        stage_id=stage_id,
        routing=routing,
        keys=("threshold_margin", "margin"),
        minimum=0.0,
        maximum=1.0,
        integer=False,
    ))
    return issues


def _routing_string_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip().lower().replace("-", "_") for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip().lower().replace("-", "_") for item in value if str(item).strip()]
    return [str(value).strip().lower().replace("-", "_")]


def _validate_stage_routing_number(
    *,
    stage_id: str,
    routing: dict[str, Any],
    keys: tuple[str, ...],
    minimum: float,
    maximum: float | None,
    integer: bool,
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for key in keys:
        if key not in routing:
            continue
        raw_value = routing.get(key)
        if raw_value is None:
            continue
        try:
            value = int(raw_value) if integer else float(raw_value)
        except (TypeError, ValueError):
            issues.append(PreflightIssue(
                level="error",
                code="invalid_stage_routing_number",
                message=f"Resolved stage `{stage_id}` routing.{key} must be numeric, got {raw_value!r}.",
            ))
            continue
        if value < minimum or (maximum is not None and value > maximum):
            upper = f" and <= {maximum}" if maximum is not None else ""
            issues.append(PreflightIssue(
                level="error",
                code="stage_routing_number_out_of_range",
                message=f"Resolved stage `{stage_id}` routing.{key} must be >= {minimum}{upper}, got {raw_value!r}.",
            ))
    return issues


def checker_request_metadata(request: CheckerRequest) -> dict[str, Any]:
    return {
        "checker_selection_mode": request.selection_mode,
        "checker_names": list(request.checker_names),
        "audit_intent": request.audit_intent,
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
        "objective": dict(plan.objective),
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
        "routing": {
            "mode": "all_samples",
        },
    }


def _resolve_llm_checker_plan(
    *,
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    defaults_by_name: dict[str, CheckerConfig],
    tool_defaults: dict,
    resource_tier: str,
    source: str,
    llm: Any | None,
    llm_model: str,
    logger: Any | None,
    data_profile: dict[str, Any] | None,
) -> ResolvedCheckerPlan:
    if llm is None or not llm_model:
        return _resolve_deterministic_single_stage_plan(
            request=request,
            capability_set=capability_set,
            defaults_by_name=defaults_by_name,
            resource_tier=resource_tier,
            source=source,
            reason="llm_resolver_unavailable",
        )

    prompt = build_llm_resolver_prompt(
        request=request,
        capability_set=capability_set,
        resource_tier=resource_tier,
        tool_defaults=tool_defaults,
        data_profile=data_profile,
    )
    try:
        if hasattr(llm, "generate_json"):
            raw_plan = llm.generate_json(
                llm_model,
                prompt,
                temperature=0.0,
            )
        else:
            content = llm.generate(
                llm_model,
                prompt,
                system_prompt="You must respond with valid JSON only.",
                temperature=0.0,
            )
            raw_plan = _load_json_object(content)
        return _resolved_plan_from_llm_output(
            raw_plan=raw_plan,
            request=request,
            capability_set=capability_set,
            defaults_by_name=defaults_by_name,
            source=source,
        )
    except Exception as exc:
        if logger is not None and hasattr(logger, "warning"):
            logger.warning(f"SecurityAuditTool: LLM resolver failed, falling back to deterministic single-stage plan: {exc}")
        return _resolve_deterministic_single_stage_plan(
            request=request,
            capability_set=capability_set,
            defaults_by_name=defaults_by_name,
            resource_tier=resource_tier,
            source=source,
            reason="llm_resolver_failed",
        )


def _resolve_deterministic_single_stage_plan(
    *,
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    defaults_by_name: dict[str, CheckerConfig],
    resource_tier: str,
    source: str,
    reason: str,
) -> ResolvedCheckerPlan:
    names, degradations = _recommend_checker_names(
        request=request,
        capability_set=capability_set,
        default_configs=list(defaults_by_name.values()),
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
        degradations=[
            {
                "reason": reason,
                "from": "llm",
                "to": "deterministic_single_stage",
            },
            *degradations,
        ],
        source=source,
    )


def _resolved_plan_from_llm_output(
    *,
    raw_plan: dict[str, Any],
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    defaults_by_name: dict[str, CheckerConfig],
    source: str,
) -> ResolvedCheckerPlan:
    if not isinstance(raw_plan, dict):
        raise ValueError("LLM resolver output must be a JSON object.")

    raw_stages = raw_plan.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValueError("LLM resolver output must include a non-empty stages list.")

    stages: list[dict[str, Any]] = []
    selected_names: list[str] = []
    skipped: list[dict[str, str]] = []
    degradations: list[dict[str, str]] = []

    for index, raw_stage in enumerate(raw_stages, start=1):
        if not isinstance(raw_stage, dict):
            degradations.append({
                "reason": "llm_stage_not_object",
                "from": f"stage_{index}",
                "to": "skip_stage",
            })
            continue
        stage, stage_names, stage_skipped, stage_degradation = _normalize_llm_stage(
            raw_stage=raw_stage,
            capability_set=capability_set,
            index=index,
        )
        stages.append(stage)
        selected_names.extend(stage_names)
        skipped.extend(stage_skipped)
        if stage_degradation:
            degradations.append(stage_degradation)

    selected_names = _unique_in_order(selected_names)
    if not selected_names:
        raise ValueError("LLM resolver selected no allowed checkers.")

    checker_configs, config_skipped = _checker_configs_from_names(
        names=selected_names,
        defaults_by_name=defaults_by_name,
        capability_set=capability_set,
    )

    return ResolvedCheckerPlan(
        strategy="llm",
        checker_configs=checker_configs,
        skipped_checkers=_dedupe_skip_records(skipped + config_skipped),
        degradations=_dedupe_skip_records(degradations),
        source=source,
        objective=_normalize_objective(raw_plan.get("objective"), stages),
        stages=stages,
        request=request,
        capability_set=capability_set,
    )


def _load_json_object(content: str) -> dict[str, Any]:
    cleaned = str(content or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("LLM resolver output must be a JSON object.")
    return parsed


def _normalize_llm_stage(
    *,
    raw_stage: dict[str, Any],
    capability_set: CheckerCapabilitySet,
    index: int,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]], dict[str, str] | None]:
    stage_id = _clean_stage_id(raw_stage.get("id"), index)
    raw_checker_names = raw_stage.get("checkers")
    checker_names = raw_checker_names if isinstance(raw_checker_names, list) else []
    allowed_stage_names: list[str] = []
    skipped: list[dict[str, str]] = []

    for raw_name in checker_names:
        if not isinstance(raw_name, str) or not raw_name.strip():
            skipped.append({"name": str(raw_name), "reason": "invalid_checker_name"})
            continue
        name = raw_name.strip()
        capability = capability_set.get(name)
        if capability and capability.allowed:
            allowed_stage_names.append(name)
            continue
        skipped.append({
            "name": name,
            "reason": capability.blocked_reason if capability else "unknown_checker",
        })

    allowed_stage_names = _unique_in_order(allowed_stage_names)
    degradation = None
    if not allowed_stage_names and (checker_names or stage_id != "review"):
        degradation = {
            "reason": f"llm_stage_{stage_id}_has_no_allowed_checkers",
            "from": stage_id,
            "to": "skip_stage",
        }

    risk_focus = _coerce_string_list(raw_stage.get("risk_focus"))
    if not risk_focus:
        risk_focus = _risk_focus_from_checker_names(allowed_stage_names)

    stage = {
        "id": stage_id,
        "name": str(raw_stage.get("name") or stage_id),
        "type": str(raw_stage.get("type") or stage_id),
        "risk_focus": risk_focus,
        "checkers": allowed_stage_names,
        "routing": _normalize_routing(raw_stage.get("routing")),
        "purpose": str(raw_stage.get("purpose") or ""),
    }
    return stage, allowed_stage_names, skipped, degradation



def _risk_focus_from_checker_names(checker_names: list[str]) -> list[str]:
    risks: list[str] = []
    for checker_name in checker_names:
        risk_type = _risk_type(checker_name)
        if risk_type:
            risks.append(risk_type)
    return _unique_in_order(risks)


def _normalize_objective(raw_objective: Any, stages: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw_objective, dict):
        return _default_progressive_workflow_objective(stages)
    return {
        "goal": str(raw_objective.get("goal") or "security_audit"),
        "covered_risk_types": _coerce_string_list(
            raw_objective.get("covered_risk_types")
        ) or _unique_in_order([
            risk
            for stage in stages
            for risk in stage.get("risk_focus", [])
        ]),
        "optimization": _coerce_string_list(raw_objective.get("optimization")),
    }


def _default_progressive_workflow_objective(stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "goal": "full_risk_coverage",
        "covered_risk_types": _unique_in_order([
            risk
            for stage in stages
            for risk in stage.get("risk_focus", [])
        ]),
        "optimization": ["coverage"],
    }


def _normalize_routing(raw_routing: Any) -> dict[str, Any]:
    routing = dict(raw_routing) if isinstance(raw_routing, dict) else {}
    routing["mode"] = str(routing.get("mode") or "all_samples")
    if routing.get("source_stage_id"):
        routing["source_stage_id"] = str(routing["source_stage_id"])
    if "rules" in routing and not isinstance(routing["rules"], list):
        routing["rules"] = [str(routing["rules"])]
    return routing


def _clean_stage_id(value: Any, index: int) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"stage_{index}"


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            items.append(item.strip())
    return _unique_in_order(items)



def build_progressive_workflow_stages(
    capability_set: CheckerCapabilitySet,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]], list[dict[str, str]]]:
    stages: list[dict[str, Any]] = []
    selected_names: list[str] = []
    skipped: list[dict[str, str]] = []
    degradations: list[dict[str, str]] = []

    for template in _PROGRESSIVE_WORKFLOW_STAGE_CHECKERS:
        stage, stage_names, stage_skipped, stage_degradation = build_progressive_workflow_stage(
            template=template,
            capability_set=capability_set,
        )
        stages.append(stage)
        selected_names.extend(stage_names)
        skipped.extend(stage_skipped)
        if stage_degradation:
            degradations.append(stage_degradation)

    return stages, _unique_in_order(selected_names), _dedupe_skip_records(skipped), degradations


def build_progressive_workflow_stage(
    *,
    template: dict[str, Any],
    capability_set: CheckerCapabilitySet,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]], dict[str, str] | None]:
    allowed_stage_names: list[str] = []
    skipped: list[dict[str, str]] = []

    for name in template["checkers"]:
        capability = capability_set.get(name)
        if capability and capability.allowed:
            allowed_stage_names.append(name)
            continue
        skipped.append({
            "name": name,
            "reason": capability.blocked_reason if capability else "unknown_checker",
        })

    degradation = None
    if not allowed_stage_names and template["checkers"]:
        degradation = {
            "reason": f"progressive_workflow_stage_{template['id']}_has_no_allowed_checkers",
            "from": template["id"],
            "to": "skip_stage",
        }

    stage = {
        "id": template["id"],
        "name": template["name"],
        "type": template["type"],
        "risk_focus": list(template["risk_focus"]),
        "checkers": allowed_stage_names,
        "routing": dict(template["routing"]),
        "purpose": template["purpose"],
    }
    return stage, allowed_stage_names, skipped, degradation


def _unique_in_order(names: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def _dedupe_skip_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        name = record.get("name", "")
        reason = record.get("reason", "")
        key = (name, reason)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def build_llm_resolver_prompt(
    *,
    request: CheckerRequest,
    capability_set: CheckerCapabilitySet,
    resource_tier: str,
    tool_defaults: dict,
    data_profile: dict[str, Any] | None = None,
) -> str:
    resolver_input = {
        "request": checker_request_metadata(request),
        "runtime_policy": {"resource_tier": resource_tier},
        "data_profile": data_profile or {},
        "capability_set": {
            "allowed_checker_names": capability_set.allowed_names(),
            "blocked_checkers": capability_set.blocked_metadata(),
            "allowed_checkers": [
                _checker_card_for_prompt(capability)
                for capability in capability_set.capabilities.values()
                if capability.allowed
            ],
        },
        "risk_weights": tool_defaults.get("risk_weights", {}),
        "stage_pattern_reference": _PROGRESSIVE_WORKFLOW_STAGE_CHECKERS,
    }
    return LLM_RESOLVER_PROMPT_TEMPLATE.format(
        output_schema=json.dumps(LLM_RESOLVER_OUTPUT_SCHEMA, ensure_ascii=False, indent=2),
        resolver_input=json.dumps(resolver_input, ensure_ascii=False, indent=2),
    )


def _checker_card_for_prompt(capability: CheckerCapability) -> dict[str, Any]:
    return {
        "name": capability.name,
        "checker_type": capability.checker_type,
        "risk_type": capability.risk_type,
        "required_tier": capability.required_tier,
        "params": capability.params,
        **_checker_planner_metadata(capability.name),
    }


def _checker_planner_metadata(checker_name: str) -> dict[str, Any]:
    try:
        checker_cls = CheckerRegistry.get(checker_name)
    except KeyError:
        return {}
    metadata = getattr(checker_cls, "planner_metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


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
    intent = request.audit_intent.lower()

    fast_markers = ("fast", "quick", "low cost", "low-cost", "cheap", "light", "成本", "快速", "低成本")
    broad_markers = ("accurate", "accuracy", "coverage", "strong", "full", "全面", "准确", "覆盖")

    if any(marker in intent for marker in fast_markers):
        names = [name for name in _RULE_BASED_CHECKERS if name in allowed]
        if names:
            return names, []

    if any(marker in intent for marker in broad_markers):
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
