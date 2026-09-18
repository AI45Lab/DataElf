from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

import pytest

from dataelf.discovery.agent_resources import discover_domain_resources, resolve_domain_resources
from dataelf.discovery.contracts import AgentResources, DiscoveryContext, DomainManifest, JobSpec
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


def test_pi_command_loads_only_explicit_domain_resources(tmp_path: Path) -> None:
    extension = tmp_path / "example.mjs"
    extension.write_text("export default function(pi) {}", encoding="utf-8")
    context = type("Context", (), {"agent_resources": AgentResources(extensions=[extension]),})()
    command = PiCliInsightsExplorer(pi_binary="fake")._build_command("fake", Path("/tmp/prompt.md"), context, {})
    assert "--no-extensions" in command
    assert command[command.index("--extension") + 1] == str(extension)
    assert "--no-skills" in command


def test_ai_index_skill_reaches_real_pi_loader(tmp_path: Path) -> None:
    """Use Pi's real parser, without API calls, after DataElf resolves CLI paths."""
    repo = Path(__file__).resolve().parents[1]
    loader = repo / "node_modules/@earendil-works/pi-coding-agent/dist/core/skills.js"
    node = shutil.which("node")
    if not node or not loader.is_file():
        pytest.skip("Run dataelf setup to enable the real Pi skill-loader integration test")
    skill = tmp_path / "domain/pi/skills/ai-index-evidence-audit/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: ai-index-evidence-audit\ndescription: Test skill.\n---\nUse it.\n", encoding="utf-8")
    resources = AgentResources(skills=[skill])
    context = DiscoveryContext(
        workspace_path=str(tmp_path), spec=JobSpec(domain="fake", objective="test"),
        manifest=DomainManifest(domain="fake", version="1", display_name="Fake", plugin="x:y"),
        agent_resources=resources,
    )
    command = PiCliInsightsExplorer(pi_binary="fake")._build_command(
        "fake", tmp_path / "prompt.md", context, {},
    )
    paths = [command[i + 1] for i, value in enumerate(command) if value == "--skill"]
    assert len(paths) == 1
    assert Path(paths[0]).is_relative_to(tmp_path / "domain")
    # A skill in Pi's ambient project directory must not leak into this run.
    ambient = tmp_path / ".pi/skills/unselected/SKILL.md"
    ambient.parent.mkdir(parents=True)
    ambient.write_text("---\nname: unselected\ndescription: Should not load.\n---\nIgnore.\n", encoding="utf-8")
    script = """
const input = JSON.parse(process.argv[1]);
const {loadSkills, formatSkillsForPrompt} = await import(input.loader);
const result = loadSkills({cwd: input.cwd, agentDir: input.cwd + '/agent',
  skillPaths: input.paths, includeDefaults: false});
console.log(JSON.stringify({names: result.skills.map(s => s.name),
  prompt: formatSkillsForPrompt(result.skills), diagnostics: result.diagnostics}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps({
            "loader": loader.as_uri(), "cwd": str(tmp_path), "paths": paths,
        })], capture_output=True, text=True, check=True, timeout=30,
    )
    loaded = json.loads(result.stdout)
    assert loaded["names"] == ["ai-index-evidence-audit"]
    assert paths[0] in loaded["prompt"]
    assert loaded["diagnostics"] == []
    other = tmp_path / "domains/other"
    other.mkdir(parents=True)
    assert discover_domain_resources(other).skills == []


@pytest.mark.parametrize("injected", [False, True])
def test_workflow_keeps_selected_plugin_and_resources(tmp_path, monkeypatch, injected):
    from tests.test_discovery_mvp import _fake_registry, _write_fake_pi, _config
    from dataelf.discovery.workflow import run_job
    from dataelf.discovery.run_control import RunControl, current_run
    import dataelf.discovery.workflow as workflow

    registry = _fake_registry(tmp_path)
    root = registry.domain_path("fake")
    extension = root / "pi/extensions/domain.ts"
    extension.parent.mkdir(parents=True)
    extension.write_text("export default function() {}")
    skill = root / "pi/skills/evidence/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Evidence")
    declared = root / "explicit.ts"
    declared.write_text("export default function() {}")
    config = _config(tmp_path, _write_fake_pi(tmp_path))
    plugin = registry.load_plugin("fake", config)
    plugin.agent_resources = lambda *args: AgentResources(extensions=[declared])
    normalize = plugin.normalize_spec
    normalized = []
    def once(spec):
        assert current_run() is not None
        normalized.append(spec)
        return normalize(spec)
    plugin.normalize_spec = once
    def load(*args):
        assert not injected, "injected plugin must never be replaced"
        return plugin
    monkeypatch.setattr(registry, "load_plugin", load)
    initialized = []
    initialize = workflow._initialize_job
    def initialize_once(*args):
        initialized.append(1)
        return initialize(*args)
    monkeypatch.setattr(workflow, "_initialize_job", initialize_once)
    contexts = []
    class Explorer(PiCliInsightsExplorer):
        def run(self, job, context):
            contexts.append(context)
            return super().run(job, context)
    spec = JobSpec(domain="fake", objective="test", workflow_profile="server" if injected else "research")
    job = run_job(spec, config, registry=registry, plugin=plugin if injected else None,
                  explorer=Explorer(pi_binary=config.explorer.pi.binary),
                  control=RunControl(job_id="selected", workspace_path=tmp_path / "attempt"))
    assert job.status == "completed", job.error_message
    assert len(initialized) == len(normalized) == len(contexts) == 1
    assert contexts[0].agent_resources.extensions == ([declared] if injected else [extension, declared])
    assert contexts[0].agent_resources.skills == ([] if injected else [skill])
    command = json.loads((tmp_path / "attempt/logs/pi_command.json").read_text())["command"]
    assert str(declared) in command
    assert (str(extension) in command) is (not injected)
    assert (str(skill) in command) is (not injected)
    assert job.job_id == "selected"


def test_server_missing_components_does_not_load_research(tmp_path, monkeypatch):
    from tests.test_discovery_mvp import _fake_registry, _write_fake_pi, _config
    from dataelf.discovery.workflow import run_job
    registry = _fake_registry(tmp_path)
    monkeypatch.setattr(registry, "load_plugin", lambda *args: pytest.fail("silent research fallback"))
    job = run_job(JobSpec(domain="fake", objective="test", workflow_profile="server"),
                  _config(tmp_path, _write_fake_pi(tmp_path)), registry=registry)
    assert job.status == "failed"
    assert "requires explicit" in job.error_message
    assert (Path(job.workspace_path) / "workspace_index.json").is_file()
