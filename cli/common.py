from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.tool_registry import ToolRegistry

from agentic import AssetManager
from config import load_config
from database import create_database_strategy
from llm import LLMTraceRecorder, OpenAIProvider, TracingLLMProvider
from runtime import JobManager, RuntimeExecutor
from tools import get_global_registry

logger = logging.getLogger(__name__)


def bootstrap_environment(
    config_path: str | None = None,
    prefix: str | None = None,
    allow_experimental_tools: bool = False,
    include_candidate_tools: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config_path=config_path, prefix=prefix)
    trace_recorder = LLMTraceRecorder(env_id=_resolve_env_id(config_path, prefix))

    db = create_database_strategy(
        db_type=cfg.database.type,
        path=cfg.database.path,
        table_name=cfg.database.table_name,
        **cfg.database.to_connection_params(),
    )

    registry = get_global_registry()
    registry.clear()
    _register_default_tools(registry, config_tools=cfg.tools)
    _validate_config_tools(cfg.tools, registry)

    asset_manager = AssetManager()
    asset_manager.register_stable_tools(registry)
    if include_candidate_tools:
        asset_manager.register_candidate_tools(registry, allow_experimental=allow_experimental_tools)

    job_manager = JobManager()
    llm_provider, tool_llm_provider = _build_llm_providers(cfg, trace_recorder)
    executor = RuntimeExecutor(
        job_manager=job_manager,
        tool_registry=registry,
        config=cfg,
        database=db,
        llm_provider=llm_provider,
        tool_llm_provider=tool_llm_provider,
    )
    dataset_schemas = collect_dataset_schemas(db)

    return {
        "config": cfg,
        "database": db,
        "registry": registry,
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


def _register_default_tools(registry: ToolRegistry, config_tools: list[str] | None = None) -> dict[str, str]:
    """Register all built-in tools. Returns a dict of {tool_name: error} for tools that failed."""
    _TOOL_MODULES: list[tuple[str, str, str]] = [
        ("tools.security_audit.tool", "SecurityAuditTool", "security_audit"),
        ("tools.scitools.bio.enzyme_acquire_tool", "EnzymeAcquireTool", "enzyme_acquire"),
        ("tools.scitools.bio.protein_analyzer_tool", "ProteinAnalyzerTool", "protein_analyzer"),
        ("tools.scoring.data_scoring_tool", "DataScoringTool", "data_scoring"),
        ("tools.select.data_select_tool", "DataSelectTool", "data_select"),
        ("tools.trajectory_skill_extraction.skillrl_skill_extraction_tool", "SkillRLSkillExtractionTool", "skillrl_skill_extraction"),
    ]

    config_set = set(config_tools or [])
    tool_errors: dict[str, str] = {}

    for module_name, class_name, tool_name in _TOOL_MODULES:
        try:
            module = __import__(module_name, fromlist=[class_name])
            tool_cls = getattr(module, class_name)
            tool = tool_cls()
            _safe_register(registry, tool)
        except Exception as e:
            tool_errors[class_name] = str(e)
            # Only warn if this tool is explicitly requested in config
            if tool_name in config_set:
                logger.warning(f"Tool {class_name} ({tool_name}) failed to load: {e}")

    return tool_errors


def _validate_config_tools(config_tools: list[str], registry: ToolRegistry) -> None:
    """Validate that every tool listed in config.yaml is registered and its dependencies are met.

    Raises RuntimeError with a clear message listing all missing tools and their errors.
    """
    if not config_tools:
        return

    registered = set(registry.list_tools())
    missing = [name for name in config_tools if name not in registered]

    if missing:
        # Map tool names to their dependency groups for actionable install hints
        _TOOL_DEPS: dict[str, str] = {
            "data_scoring": 'pip install -e ".[scoring]"',
            "data_select": 'pip install -e ".[scoring]"',
            "enzyme_acquire": 'pip install -e ".[scitools]"',
            "protein_analyzer": 'pip install -e ".[scitools]"',
        }
        dep_hints: list[str] = []
        for name in missing:
            hint = _TOOL_DEPS.get(name)
            if hint and hint not in dep_hints:
                dep_hints.append(hint)

        msg = (
            "Config declares tools that are not available:\n"
            + "\n".join(f"  - {name}" for name in missing)
        )
        if dep_hints:
            msg += "\n\nInstall the required dependency group(s):\n  " + "\n  ".join(dep_hints)
        else:
            msg += "\n\nPlease install the required dependencies or remove these tools from your config."
        raise RuntimeError(msg)


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
