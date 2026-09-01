from __future__ import annotations

from dataelf.config import DataElfConfig
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer


def create_explorer(config: DataElfConfig) -> PiCliInsightsExplorer:
    if config.explorer.type != "pi":
        raise ValueError(f"Unsupported explorer: {config.explorer.type!r}")
    pi = config.explorer.pi
    return PiCliInsightsExplorer(
        pi_binary=pi.binary,
        model=pi.model,
        mode=pi.mode,
        cwd=pi.cwd,
        timeout_seconds=pi.timeout_seconds,
        extra_args=pi.extra_args,
        log_mode=pi.log_mode,
    )


__all__ = ["create_explorer"]
