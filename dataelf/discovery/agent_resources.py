from __future__ import annotations

from pathlib import Path
from typing import Any

from dataelf.discovery.contracts import AgentResources, DomainPlugin, JobSpec


_EXTENSION_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}


def discover_domain_resources(domain_root: Path) -> AgentResources:
    """Discover only the explicit Pi resource directories owned by a domain.

    Extension discovery mirrors Pi's project convention: files directly under
    ``extensions`` and ``index.*`` files one directory below it. Skills follow
    Pi's recursive ``SKILL.md`` convention. Other files are never loaded.
    """
    pi_root = domain_root / "pi"
    extensions_root = pi_root / "extensions"
    extensions: list[Path] = []
    if extensions_root.is_dir():
        for path in sorted(extensions_root.iterdir()):
            if path.is_file() and path.suffix in _EXTENSION_SUFFIXES:
                extensions.append(path.resolve())
            elif path.is_dir():
                for candidate in sorted(path.iterdir()):
                    if candidate.is_file() and candidate.stem == "index" and candidate.suffix in _EXTENSION_SUFFIXES:
                        extensions.append(candidate.resolve())

    skills_root = pi_root / "skills"
    skills = sorted(path.resolve() for path in skills_root.rglob("SKILL.md")) if skills_root.is_dir() else []
    return AgentResources(extensions=extensions, skills=skills)


def resolve_domain_resources(
    domain_root: Path,
    plugin: DomainPlugin,
    spec: JobSpec,
    config: Any,
    *, discover: bool = True,
) -> AgentResources:
    """Merge conventional resources with an optional plugin declaration."""
    discovered = discover_domain_resources(domain_root) if discover else AgentResources()
    provider = getattr(plugin, "agent_resources", None)
    declared = provider(spec, config) if callable(provider) else AgentResources()
    if not isinstance(declared, AgentResources):
        declared = AgentResources.model_validate(declared)

    root = domain_root.resolve()
    extensions = _merge_paths(root, [*discovered.extensions, *declared.extensions], "extension")
    skills = _merge_paths(root, [*discovered.skills, *declared.skills], "skill")
    return AgentResources(extensions=extensions, skills=skills)


def _merge_paths(root: Path, paths: list[Path], kind: str) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = raw if raw.is_absolute() else root / raw
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Domain {kind} resource does not exist: {path}")
        if not path.is_relative_to(root):
            raise ValueError(f"Domain {kind} resource escapes domain directory: {path}")
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


__all__ = ["discover_domain_resources", "resolve_domain_resources"]
