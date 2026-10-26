"""Stable legacy behavior migrated from DataElf 78f2ef4.

Owned by legacy entries; new AI research must not add behavior here.
Repository license applies. Only package-root paths and boundary types adapt.
"""
from __future__ import annotations

from dataelf.config import DataElfConfig
from dataelf.discovery.legacy.pi_explorer import LegacyPiExplorer


def create_explorer(config: DataElfConfig) -> LegacyPiExplorer:
    if config.explorer.type != "pi":
        raise ValueError(f"Unsupported explorer: {config.explorer.type!r}")
    pi = config.explorer.pi
    return LegacyPiExplorer(
        pi_binary=pi.binary,
        model=pi.model,
        mode=pi.mode,
        cwd=pi.cwd,
        timeout_seconds=pi.timeout_seconds,
        extra_args=pi.extra_args,
        log_mode=pi.log_mode,
    )


__all__ = ["create_explorer"]
