from pathlib import Path

import pytest

from dataelf.discovery import pi_runtime
from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob, DomainManifest, JobSpec
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer
from dataelf_server.workflows.explorer import ServerExplorer
from tests.server.helpers import settings


@pytest.mark.parametrize("retry", [False, True])
def test_managed_pi_server_agent_does_not_require_fusion(tmp_path, monkeypatch, retry):
    binary = tmp_path / "pi"
    binary.write_text("#!/bin/sh\ntouch \"$DATAELF_JOB_WORKSPACE/executed\"\n")
    binary.chmod(0o755)
    monkeypatch.setattr(pi_runtime, "local_pi_binary", lambda: binary)
    agent = tmp_path / "isolated-agent"
    agent.mkdir()
    workspace = tmp_path / "attempt"
    workspace.mkdir()
    prompt = workspace / "prompt.md"
    prompt.write_text("Produce the declared output.")
    spec = JobSpec(domain="example", objective="test", workflow_profile="server")
    job = DiscoveryJob(job_id="test", spec=spec, workspace_path=str(workspace))
    context = DiscoveryContext(
        workspace_path=str(workspace), spec=spec, prompt_path=str(prompt),
        manifest=DomainManifest(domain="example", version="1", display_name="Example", plugin="example:plugin"),
        env={"PI_CODING_AGENT_DIR": str(agent)},
    )

    # The normal research executor still refuses an incomplete managed runtime.
    research = PiCliInsightsExplorer(pi_binary=str(binary), cwd=tmp_path, log_prefix="research_probe")
    failed = research.run(job, context)
    assert failed.error_code == "EXPLORER_RUNTIME_NOT_READY"
    assert not (workspace / "executed").exists()
    assert "logs/research_probe_stderr.log" in {ref.path for ref in failed.artifacts}

    # The server-assembled executor uses the same binary without loading Fusion.
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / "state")
    cfg.core.explorer.pi.binary = str(binary)
    runner = ServerExplorer(cfg)._runner("scope_v2", retry=retry)
    result = runner.run(job, context)
    assert result.status == "completed"
    assert (workspace / "executed").exists()
    prefix = "pi_synthesis_retry" if retry else "pi"
    assert f"logs/{prefix}_stdout.log" in {ref.path for ref in result.artifacts}
    assert not (tmp_path / "npm").exists()

    package = tmp_path / "npm/node_modules/@quarkos/pi-fusion/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{}')
    assert research.run(job, context).error_code == "EXPLORER_RUNTIME_NOT_READY"
    web = tmp_path / "npm/node_modules/pi-web-access/package.json"
    web.parent.mkdir(parents=True)
    web.write_text('{}')
    assert research.run(job, context).status == "completed"
    # Even if managed packages exist, a Server runner must not load them.
    extension = package.parent / "index.js"
    extension.write_text("export default function() {}")
    package.write_text('{"pi":{"extensions":["index.js"]}}')
    command = runner._build_command(str(binary), prompt, context, context.env)
    assert str(extension) not in command
    assert str(extension) in research._build_command(str(binary), prompt, context, context.env)


def test_no_package_requirements_does_not_allow_missing_pi(tmp_path):
    assert not pi_runtime.runtime_ready_for_process(None, tmp_path, {}, required_packages=())
