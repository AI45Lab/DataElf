from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SkillPackage:
    name: str
    description: str
    path: Path
    frontmatter: dict[str, Any]
    body: str

    @property
    def allowed_tools(self) -> list[str]:
        raw = self.frontmatter.get("allowed-tools", self.frontmatter.get("allowed_tools", []))
        return raw if isinstance(raw, list) else []

    @property
    def usage_summary(self) -> str:
        for heading in ("Usage Instructions", "Usage", "When To Use"):
            text = _extract_section(self.body, heading)
            if text:
                return _first_paragraph(text)
        return _first_paragraph(self.body)

    @property
    def clarification_summary(self) -> str:
        text = _extract_section(self.body, "Clarification Hints")
        return _first_paragraph(text) if text else ""

    def planner_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "usage_summary": self.usage_summary,
            "allowed_tools": self.allowed_tools,
            "clarification_hints": self.clarification_summary,
        }


class SkillRegistry:
    def __init__(
        self,
        search_paths: list[str | Path] | None = None,
        enabled_skills: list[str] | None = None,
    ) -> None:
        self.search_paths = [Path(path) for path in (search_paths or [])]
        self.enabled_skills = set(enabled_skills or [])
        self._skills: dict[str, SkillPackage] = {}

    def discover(self) -> None:
        self._skills.clear()
        for root in self.search_paths:
            if not root.exists():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                package = load_skill_package(skill_md.parent)
                if self.enabled_skills and package.name not in self.enabled_skills:
                    continue
                self._skills[package.name] = package

    def get(self, name: str) -> SkillPackage | None:
        return self._skills.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._skills)

    def list_planner_views(self) -> list[dict[str, Any]]:
        return [self._skills[name].planner_view() for name in self.list_names()]

    def load_full_instructions(self, name: str) -> str:
        package = self._require(name)
        return package.body

    def manifest(self, name: str) -> dict[str, list[str]]:
        package = self._require(name)
        return {
            "references": _relative_files(package.path / "references"),
            "scripts": _relative_files(package.path / "scripts"),
            "assets": _relative_files(package.path / "assets"),
        }

    def load_documentation_entries(self, name: str, max_len: int = 2000) -> list[dict[str, str]]:
        package = self._require(name)
        entries = [{
            "skill_name": name,
            "path": str(package.path / "SKILL.md"),
            "content": package.body[:max_len].strip(),
            "kind": "skill",
        }]
        references_dir = package.path / "references"
        if references_dir.exists():
            for reference in sorted(references_dir.glob("*.md")):
                entries.append({
                    "skill_name": name,
                    "path": str(reference),
                    "content": reference.read_text(encoding="utf-8")[:max_len].strip(),
                    "kind": "reference",
                })
        return entries

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name, package in self._skills.items():
            if not package.description:
                errors.append(f"{name}: missing description")
            if not package.body.strip():
                errors.append(f"{name}: empty SKILL.md body")
        return errors

    def _require(self, name: str) -> SkillPackage:
        package = self.get(name)
        if package is None:
            raise KeyError(f"Skill not found: {name}")
        return package


def builtin_skill_root() -> Path:
    return Path(__file__).parent.parent / "skills"


def load_skill_package(path: str | Path) -> SkillPackage:
    root = Path(path)
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Missing SKILL.md: {skill_md}")
    text = skill_md.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    name = str(frontmatter.get("name") or root.name)
    description = str(frontmatter.get("description") or "").strip()
    return SkillPackage(
        name=name,
        description=description,
        path=root,
        frontmatter=frontmatter,
        body=body.strip(),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.DOTALL)
    if not match:
        return {}, text
    data = yaml.safe_load(match.group(1)) or {}
    return data if isinstance(data, dict) else {}, match.group(2)


def _extract_section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def _first_paragraph(text: str, max_len: int = 500) -> str:
    compact = re.sub(r"\s+", " ", text.strip())
    return compact[:max_len].strip()


def _relative_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(str(item.relative_to(path.parent)) for item in path.rglob("*") if item.is_file())
