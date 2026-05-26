from __future__ import annotations

from pathlib import Path

import click

from config import load_config
from runtime.skill_registry import builtin_skill_root
from runtime.skill_registry import SkillRegistry, load_skill_package


@click.group(name="skills")
def skills_cmd() -> None:
    """Inspect and validate DataElf skills."""


@skills_cmd.command("list")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to config file")
@click.option("--prefix", "-p", type=str, help="Config file prefix")
def list_skills(config: str | None, prefix: str | None) -> None:
    registry = _load_registry(config, prefix)
    for view in registry.list_planner_views():
        click.echo(f"{view['name']}: {view.get('description', '')}")


@skills_cmd.command("inspect")
@click.argument("name")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to config file")
@click.option("--prefix", "-p", type=str, help="Config file prefix")
def inspect_skill(name: str, config: str | None, prefix: str | None) -> None:
    registry = _load_registry(config, prefix)
    package = registry.get(name)
    if package is None:
        raise click.ClickException(f"Skill not found: {name}")
    click.echo((package.path / "SKILL.md").read_text(encoding="utf-8"))


@skills_cmd.command("validate")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
def validate_skill(path: str) -> None:
    root = Path(path)
    try:
        package = load_skill_package(root)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    registry = SkillRegistry([root.parent], enabled_skills=[package.name])
    registry.discover()
    errors = registry.validate()
    if errors:
        raise click.ClickException("\n".join(errors))
    click.echo(f"valid: {package.name}")


def _load_registry(config: str | None, prefix: str | None) -> SkillRegistry:
    cfg = load_config(config_path=config, prefix=prefix)
    paths = [builtin_skill_root(), *[Path(path) for path in cfg.skill_paths]]
    registry = SkillRegistry(paths, enabled_skills=cfg.skills)
    registry.discover()
    return registry
