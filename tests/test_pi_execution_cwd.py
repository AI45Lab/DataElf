"""Explicit task cwd must not relocate Pi's provider/configuration root."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataelf.discovery.contracts import (
    DiscoveryContext,
    DiscoveryJob,
    DomainManifest,
    JobSpec,
)
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer


def setup(tmp_path, execution):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nested").mkdir()
    prompt = workspace / "prompt.md"
    prompt.write_text("Task")
    spec = JobSpec(domain="finance", objective="Task")
    context = DiscoveryContext(
        workspace_path=str(workspace),
        spec=spec,
        manifest=DomainManifest(
            domain="finance", version="1", display_name="Test", plugin="test"
        ),
        prompt_path=str(prompt),
        execution=execution,
    )
    job = DiscoveryJob(job_id="cwd", spec=spec, workspace_path=str(workspace))
    explorer = PiCliInsightsExplorer(
        pi_binary="/bin/true", cwd=runtime, approve_project=False
    )
    return explorer, job, context, runtime, workspace


@pytest.mark.parametrize("selection", [None, ".", "nested", "absolute"])
def test_launch_separates_runtime_and_workspace(tmp_path, monkeypatch, selection):
    import dataelf.discovery.pi_cli_explorer as module

    explorer, job, context, runtime, workspace = setup(tmp_path, {})
    if selection is not None:
        context.execution["cwd"] = (
            str(workspace) if selection == "absolute" else selection
        )
    seen = {}

    def ready(binary, cwd, env, **kwargs):
        seen["runtime"] = cwd
        return True

    def process(command, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "runtime_ready_for_process", ready)
    monkeypatch.setattr(module, "_run_pi_process", process)
    assert explorer.run(job, context).status == "completed"
    expected = (
        runtime
        if selection is None
        else workspace / "nested" if selection == "nested" else workspace
    )
    assert seen["cwd"] == expected
    assert seen["runtime"] == runtime
    assert seen["env"]["PI_CODING_AGENT_DIR"] == str(runtime / ".pi/agent")
    assert seen["env"]["NPM_CONFIG_CACHE"] == str(runtime / ".pi/npm-cache")
    log = json.loads((workspace / "logs/pi_command.json").read_text())
    assert log["cwd"] == str(expected)
    assert log["runtime_cwd"] == str(runtime)


@pytest.mark.parametrize(
    "selection", ["", 42, "../runtime", "missing", "prompt.md", "escape"]
)
def test_invalid_cwd_rejected_before_launch(tmp_path, monkeypatch, selection):
    explorer, job, context, runtime, workspace = setup(tmp_path, {"cwd": selection})
    (workspace / "escape").symlink_to(runtime, target_is_directory=True)
    monkeypatch.setattr(
        explorer,
        "_resolve_binary",
        lambda: pytest.fail("must reject before process setup"),
    )
    result = explorer.run(job, context)
    assert result.error_code == "PI_WORKING_DIRECTORY_INVALID"
