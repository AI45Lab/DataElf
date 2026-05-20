from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from config import PreflightIssue, RuntimePolicy

from .config import CheckerConfig


_MODEL_PATH_KEYS = {
    "HarmfulContentClassifier": "harmful_content_classifier",
    "JailbreakClassifier": "jailbreak_classifier",
    "PromptInjectionClassifier": "prompt_injection_classifier",
    "BiasClassifier": "bias_classifier",
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


def validate_selected_checkers(
    *,
    checker_configs: list[CheckerConfig],
    tool_defaults: dict[str, Any],
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for checker_config in checker_configs:
        if not checker_config.enabled:
            continue
        issues.extend(validate_checker_network_availability(
            checker_config=checker_config,
            tool_defaults=tool_defaults,
            runtime_policy=runtime_policy,
            context_config=context_config,
        ))
        issues.extend(validate_checker_resource_tier_availability(
            checker_config=checker_config,
            tool_defaults=tool_defaults,
            runtime_policy=runtime_policy,
        ))
    return issues


def validate_checker_network_availability(
    *,
    checker_config: CheckerConfig,
    tool_defaults: dict[str, Any],
    runtime_policy: RuntimePolicy,
    context_config: dict[str, Any],
) -> list[PreflightIssue]:
    if not runtime_policy.offline:
        # TODO: (network_mode) Add best-effort dependency warnings for online
        # mode once checker dependency metadata is finalized.
        return []

    name = checker_config.name

    if name.endswith("LLMJudge") and not _has_local_llm_config(context_config):
        return [PreflightIssue(
            level="error",
            code="offline_checker_missing_llm",
            checker_name=name,
            message=(
                "LLM judge checkers require a configured local or intranet LLM "
                "endpoint when deployment.network_mode=offline."
            ),
        )]

    if name in _MODEL_PATH_KEYS:
        model_key = _MODEL_PATH_KEYS[name]
        model_path = _resolve_model_path(checker_config, tool_defaults, model_key)
        if not model_path:
            return [PreflightIssue(
                level="error",
                code="offline_checker_missing_model_path",
                checker_name=name,
                message=(
                    "Offline model-based checker requires a local model path. "
                    f"Set tool_defaults.security_audit.models.{model_key} or pass "
                    "checker params.model_name_or_path."
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

    # TODO: (network_mode) Extend offline checks for Detoxify local cache,
    # local_files_only propagation, and checker implementations that may still
    # trigger implicit downloads.
    return []


def validate_checker_resource_tier_availability(
    *,
    checker_config: CheckerConfig,
    tool_defaults: dict[str, Any],
    runtime_policy: RuntimePolicy,
) -> list[PreflightIssue]:
    # TODO: (resource_tier) Intern-owned implementation. Keep this flexible:
    # define checker min_tier metadata and default checker pools for
    # light/standard/full. Keep provenance simple for now: this layer validates
    # the final selected checkers rather than tracking whether they came from
    # user text, generated DSL, or config defaults.
    return []


def resolve_default_checkers_for_resource_tier(resource_tier: str) -> list[str]:
    # TODO: (resource_tier) Replace this placeholder with light/standard/full
    # default checker sets and, later, funnel-routing strategy selection.
    return [
        "PIIRule",
        "SecretRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "BiasKeywordRule",
    ]


def _has_local_llm_config(context_config: dict[str, Any]) -> bool:
    tool_llm = _get_section(context_config, "tool_llm")
    agent = _get_section(context_config, "agent")
    return bool(
        (_get_value(tool_llm, "model") and _get_value(tool_llm, "base_url"))
        or (_get_value(agent, "model") and _get_value(agent, "base_url"))
    )


def _resolve_model_path(
    checker_config: CheckerConfig,
    tool_defaults: dict[str, Any],
    model_key: str,
) -> str | None:
    explicit = checker_config.params.get("model_name_or_path")
    if explicit:
        return str(explicit)
    models = tool_defaults.get("models") if isinstance(tool_defaults.get("models"), dict) else {}
    value = models.get(model_key)
    return str(value) if value else None


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
