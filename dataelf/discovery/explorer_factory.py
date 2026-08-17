from __future__ import annotations

from dataelf.config import DataElfConfig
from dataelf.discovery.deepagents_code_cli_explorer import DeepAgentsCodeCliInsightsExplorer
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer


DCODE_EXPLORER_ALIASES = {"dcode", "deepagentscode", "deepagents-code", "deepagents_code"}
PI_EXPLORER_ALIASES = {"pi", "pi-cli", "picli"}


def create_insights_explorer(config: DataElfConfig):
    name = normalize_insights_explorer_name(config.insights_explorer)
    if name == "deepagentscode":
        return DeepAgentsCodeCliInsightsExplorer(
            dcode_binary=config.dcode_binary,
            shell_allow_list=config.dcode_shell_allow_list,
            extra_args=config.dcode_extra_args,
            stream_logs=config.dcode_stream_logs,
        )
    if name == "pi":
        return PiCliInsightsExplorer(
            pi_binary=config.pi_binary,
            model=config.pi_model,
            mode=config.pi_mode,
            cwd=config.pi_cwd,
            timeout_seconds=config.pi_timeout_seconds,
            extra_args=config.pi_extra_args,
            stream_logs=config.pi_stream_logs,
            log_mode=config.pi_log_mode,
        )
    raise ValueError(
        f"Unsupported DATAELF_INSIGHTS_EXPLORER: {config.insights_explorer!r}. "
        "Use 'deepagentscode'/'dcode' or 'pi'."
    )


def normalize_insights_explorer_name(value: str | None) -> str:
    raw = (value or "deepagentscode").strip().lower()
    if raw in DCODE_EXPLORER_ALIASES:
        return "deepagentscode"
    if raw in PI_EXPLORER_ALIASES:
        return "pi"
    return raw


def is_pi_family_explorer(value: str | None) -> bool:
    return normalize_insights_explorer_name(value) == "pi"
