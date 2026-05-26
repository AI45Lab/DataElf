from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.tool_registry import ToolRegistry

from agentic import AssetManager
from config import (
    apply_runtime_environment,
    build_runtime_policy,
    handle_preflight_issues,
    load_config,
    run_global_preflight,
)
from database import create_database_strategy
from llm import LLMTraceRecorder, OpenAIProvider, TracingLLMProvider
from runtime import JobManager, RuntimeExecutor
from runtime.skill_registry import SkillRegistry, builtin_skill_root
from runtime.skill_runtime import SkillRuntime
from tools import get_global_registry

logger = logging.getLogger(__name__)


def bootstrap_environment(
    config_path: str | None = None,
    prefix: str | None = None,
    allow_experimental_tools: bool = False,
    include_candidate_tools: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config_path=config_path, prefix=prefix)
    runtime_policy = build_runtime_policy(cfg)
    apply_runtime_environment(runtime_policy)
    handle_preflight_issues(
        run_global_preflight(cfg, runtime_policy),
        strict=runtime_policy.strict_preflight,
        logger=logger,
    )
    trace_recorder = LLMTraceRecorder(
        env_id=_resolve_env_id(config_path, prefix),
        enabled=cfg.llm_tracing.enabled,
        output_dir=Path(cfg.llm_tracing.output_dir),
    )

    db = create_database_strategy(
        db_type=cfg.database.type,
        path=cfg.database.path,
        table_name=cfg.database.table_name,
        **cfg.database.to_connection_params(),
    )

    registry = get_global_registry()
    registry.clear()
    _register_builtin_skill_backends(registry, enabled_skills=cfg.skills)

    skill_search_paths = [builtin_skill_root(), *[Path(path) for path in cfg.skill_paths]]
    skill_registry = SkillRegistry(skill_search_paths, enabled_skills=cfg.skills)
    skill_registry.discover()
    _validate_config_skills(cfg.skills, skill_registry, registry)

    asset_manager = AssetManager()
    asset_manager.register_stable_tools(registry)
    if include_candidate_tools:
        asset_manager.register_candidate_tools(registry, allow_experimental=allow_experimental_tools)

    job_manager = JobManager()
    llm_provider, tool_llm_provider = _build_llm_providers(cfg, trace_recorder)
    executor = RuntimeExecutor(
        job_manager=job_manager,
        tool_registry=registry,
        skill_runtime=SkillRuntime(skill_registry=skill_registry, tool_registry=registry),
        config=cfg,
        database=db,
        llm_provider=llm_provider,
        tool_llm_provider=tool_llm_provider,
    )
    dataset_schemas = collect_dataset_schemas(db)

    return {
        "config": cfg,
        "runtime_policy": runtime_policy,
        "database": db,
        "registry": registry,
        "skill_registry": skill_registry,
        "job_manager": job_manager,
        "executor": executor,
        "asset_manager": asset_manager,
        "llm_provider": llm_provider,
        "tool_llm_provider": tool_llm_provider,
        "trace_recorder": trace_recorder,
        "dataset_schemas": dataset_schemas,
    }


def collect_dataset_schemas(database: Any) -> dict[str, list[str]]:
    dataset_schemas: dict[str, list[str]] = {}
    try:
        for table_name in database.list_tables():
            sample = database.read_table(table_name, limit=1)
            if sample:
                dataset_schemas[table_name] = list(sample[0].keys())
    except Exception:
        pass
    return dataset_schemas


def _register_builtin_skill_backends(registry: ToolRegistry, enabled_skills: list[str] | None = None) -> dict[str, str]:
    """Register built-in Python backends for enabled built-in skills."""
    _TOOL_MODULES: list[tuple[str, str, str]] = [
        ("tools.security_audit.tool", "SecurityAuditTool", "security_audit"),
        ("tools.scitools.bio.enzyme_acquire_tool", "EnzymeAcquireTool", "enzyme_acquire"),
        ("tools.scitools.bio.protein_analyzer_tool", "ProteinAnalyzerTool", "protein_analyzer"),
        ("tools.scoring.data_scoring_tool", "DataScoringTool", "data_scoring"),
        ("tools.select.data_select_tool", "DataSelectTool", "data_select"),
        ("tools.trajectory_skill_extraction.skillrl_skill_extraction_tool", "SkillRLSkillExtractionTool", "skillrl_skill_extraction"),
    ]

    enabled_set = set(enabled_skills or [])
    tool_errors: dict[str, str] = {}

    for module_name, class_name, tool_name in _TOOL_MODULES:
        if enabled_set and tool_name not in enabled_set:
            continue
        try:
            module = __import__(module_name, fromlist=[class_name])
            tool_cls = getattr(module, class_name)
            tool = tool_cls()
            _safe_register(registry, tool)
        except Exception as e:
            tool_errors[class_name] = str(e)
            if tool_name in enabled_set:
                logger.warning(f"Built-in skill backend {class_name} ({tool_name}) failed to load: {e}")

    return tool_errors


def _validate_config_skills(
    config_skills: list[str],
    skill_registry: SkillRegistry,
    backend_registry: ToolRegistry,
) -> None:
    """Validate enabled skills and their built-in Python backends when required."""
    if not config_skills:
        return

    discovered = set(skill_registry.list_names())
    missing = [name for name in config_skills if name not in discovered]

    if missing:
        _SKILL_DEPS: dict[str, str] = {
            "data_scoring": 'pip install -e ".[scoring]"',
            "data_select": 'pip install -e ".[scoring]"',
            "enzyme_acquire": 'pip install -e ".[scitools]"',
            "protein_analyzer": 'pip install -e ".[scitools]"',
        }
        dep_hints: list[str] = []
        for name in missing:
            hint = _SKILL_DEPS.get(name)
            if hint and hint not in dep_hints:
                dep_hints.append(hint)

        msg = (
            "Config declares skills that are not available:\n"
            + "\n".join(f"  - {name}" for name in missing)
        )
        if dep_hints:
            msg += "\n\nInstall the required dependency group(s):\n  " + "\n  ".join(dep_hints)
        else:
            msg += "\n\nPlease install the required dependencies or remove these skills from your config."
        raise RuntimeError(msg)

    backend_missing = [
        name
        for name in config_skills
        if name in _BUILTIN_SKILL_NAMES and backend_registry.get(name) is None
    ]
    if backend_missing:
        raise RuntimeError(
            "Built-in skill backend(s) are not available:\n"
            + "\n".join(f"  - {name}" for name in backend_missing)
        )


_BUILTIN_SKILL_NAMES = {
    "security_audit",
    "enzyme_acquire",
    "protein_analyzer",
    "data_scoring",
    "data_select",
    "skillrl_skill_extraction",
}


def _build_llm_providers(cfg: Any, trace_recorder: LLMTraceRecorder) -> tuple[Any, Any]:
    llm_provider = None
    tool_llm_provider = None
    if cfg.agent.type == "opencode":
        llm_provider = TracingLLMProvider(OpenAIProvider(
            api_key=cfg.agent.api_key,
            base_url=cfg.agent.base_url,
            max_retries=cfg.agent.max_retries,
            retry_delay=cfg.agent.retry_delay,
        ), trace_recorder)
    if cfg.tool_llm.is_configured():
        tool_llm_provider = TracingLLMProvider(OpenAIProvider(
            api_key=cfg.tool_llm.api_key,
            base_url=cfg.tool_llm.base_url,
            max_retries=cfg.tool_llm.max_retries,
            retry_delay=cfg.tool_llm.retry_delay,
        ), trace_recorder)
    return llm_provider, tool_llm_provider


def _resolve_env_id(config_path: str | None, prefix: str | None) -> str:
    if prefix:
        return prefix
    if config_path:
        return Path(config_path).stem
    return "default"


def _safe_register(registry: "ToolRegistry", tool: Any) -> None:
    if registry.get(tool.name) is None:
        registry.register(tool)
