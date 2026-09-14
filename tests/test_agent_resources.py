from __future__ import annotations

from pathlib import Path

from dataelf.discovery.agent_resources import discover_domain_resources, resolve_domain_resources
from dataelf.discovery.contracts import AgentResources, DomainManifest, JobSpec
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer


class _Plugin:
    manifest = DomainManifest(domain="fake", version="1", display_name="Fake", plugin="x:y")

    def agent_resources(self, spec: JobSpec, config: object) -> AgentResources:
        return AgentResources(extensions=[Path("custom/declared.mjs")])


def test_domain_resources_follow_conventions_and_plugin_declarations(tmp_path: Path) -> None:
    domain = tmp_path / "fake"
    (domain / "pi" / "extensions" / "nested").mkdir(parents=True)
    (domain / "pi" / "skills" / "analysis").mkdir(parents=True)
    (domain / "pi" / "extensions" / "main.mjs").write_text("", encoding="utf-8")
    (domain / "pi" / "extensions" / "nested" / "index.ts").write_text("", encoding="utf-8")
    (domain / "pi" / "extensions" / "nested" / "helper.ts").write_text("", encoding="utf-8")
    (domain / "pi" / "skills" / "analysis" / "SKILL.md").write_text("# skill", encoding="utf-8")
    (domain / "custom").mkdir()
    (domain / "custom" / "declared.mjs").write_text("", encoding="utf-8")

    discovered = discover_domain_resources(domain)
    assert [path.name for path in discovered.extensions] == ["main.mjs", "index.ts"]
    assert [path.name for path in discovered.skills] == ["SKILL.md"]
    merged = resolve_domain_resources(domain, _Plugin(), JobSpec(domain="fake", objective="test"), {})
    assert [path.name for path in merged.extensions] == ["main.mjs", "index.ts", "declared.mjs"]


def test_domain_resources_reject_paths_outside_domain(tmp_path: Path) -> None:
    domain = tmp_path / "fake"
    domain.mkdir()
    outside = tmp_path / "outside.mjs"
    outside.write_text("", encoding="utf-8")

    class Plugin(_Plugin):
        def agent_resources(self, spec: JobSpec, config: object) -> AgentResources:
            return AgentResources(extensions=[outside])

    try:
        resolve_domain_resources(domain, Plugin(), JobSpec(domain="fake", objective="test"), {})
    except ValueError as exc:
        assert "escapes domain directory" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("outside resource should be rejected")


def test_pi_command_loads_only_explicit_domain_resources() -> None:
    extension = Path("dataelf/domains/ai_index/pi/extensions/ai_index_tools.mjs").resolve()
    context = type("Context", (), {"agent_resources": AgentResources(extensions=[extension]),})()
    command = PiCliInsightsExplorer(pi_binary="fake")._build_command("fake", Path("/tmp/prompt.md"), context, {})
    assert "--no-extensions" in command
    assert command[command.index("--extension") + 1] == str(extension)
    assert "--no-skills" in command

