from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Config
from runtime.execution_plan import ExecutionPlanError, PLAN_VERSION, validate_execution_plan
from runtime.executor import RuntimeExecutor
from runtime.job_manager import JobManager
from runtime.skill_registry import SkillRegistry, builtin_skill_root
from runtime.skill_runtime import SkillRuntime
from tools.base_tool import BaseTool, ToolContext
from tools.tool_registry import ToolRegistry


class EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "echo_skill"

    @property
    def description(self) -> str:
        return "Echo input data."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"data": {"type": "array"}},
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs):
        return {
            "result": {"echo": kwargs["data"]},
            "metadata": {"ok": True},
            "trace": {"component": "echo"},
        }


def test_skill_registry_discovers_builtin_skills() -> None:
    registry = SkillRegistry([builtin_skill_root()])
    registry.discover()

    names = registry.list_names()
    assert "security_audit" in names
    assert "data_scoring" in names

    view = next(item for item in registry.list_planner_views() if item["name"] == "security_audit")
    assert "BaseTool" not in json.dumps(view)
    assert "SKILL.md" not in json.dumps(view)
    assert "security risks" in view["description"].lower()


def test_skill_registry_loads_full_instructions_on_demand() -> None:
    registry = SkillRegistry([builtin_skill_root()], enabled_skills=["security_audit"])
    registry.discover()

    full = registry.load_full_instructions("security_audit")
    assert "Input Expectations" in full
    assert "checker_names" in full


def test_skill_registry_loads_reference_docs_on_demand() -> None:
    registry = SkillRegistry([builtin_skill_root()], enabled_skills=["security_audit"])
    registry.discover()

    entries = registry.load_documentation_entries("security_audit", max_len=500)
    paths = [entry["path"] for entry in entries]
    assert any(path.endswith("SKILL.md") for path in paths)
    assert any(path.endswith("references/security_audit_en.md") for path in paths)
    assert any(entry["kind"] == "reference" for entry in entries)


def test_execution_plan_validation_rejects_illegal_op() -> None:
    with pytest.raises(ExecutionPlanError, match="Illegal op"):
        validate_execution_plan({
            "version": PLAN_VERSION,
            "steps": [{"id": "x", "op": "python_exec"}],
        })


def test_execution_plan_validation_rejects_unknown_variable() -> None:
    with pytest.raises(ExecutionPlanError, match="unknown variable"):
        validate_execution_plan({
            "version": PLAN_VERSION,
            "steps": [
                {
                    "id": "audit",
                    "op": "invoke_skill",
                    "skill": "security_audit",
                    "input": {"data": "$missing"},
                    "output": "result",
                }
            ],
        })


def test_execution_plan_validation_rejects_unknown_skill() -> None:
    with pytest.raises(ExecutionPlanError, match="Unknown skill"):
        validate_execution_plan(
            {
                "version": PLAN_VERSION,
                "steps": [
                    {
                        "id": "x",
                        "op": "invoke_skill",
                        "skill": "missing_skill",
                        "input": {},
                        "output": "result",
                    }
                ],
            },
            available_skills={"security_audit"},
        )


def test_external_skill_package_validation(tmp_path: Path) -> None:
    skill_dir = tmp_path / "echo_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: echo_skill
description: Echo input data.
---

## Usage Instructions

Use this fixture to echo input.
""",
        encoding="utf-8",
    )

    registry = SkillRegistry([tmp_path], enabled_skills=["echo_skill"])
    registry.discover()
    assert registry.validate() == []
    assert registry.list_planner_views()[0]["name"] == "echo_skill"


def test_runtime_executor_invokes_builtin_skill_backend(tmp_path: Path) -> None:
    skill_dir = tmp_path / "echo_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: echo_skill\ndescription: Echo input data.\n---\n\n## Usage Instructions\nEcho.",
        encoding="utf-8",
    )
    skill_registry = SkillRegistry([tmp_path], enabled_skills=["echo_skill"])
    skill_registry.discover()
    tool_registry = ToolRegistry()
    tool_registry.register(EchoTool())
    executor = RuntimeExecutor(
        job_manager=JobManager(tmp_path / "jobs"),
        tool_registry=tool_registry,
        skill_runtime=SkillRuntime(skill_registry, tool_registry),
        config=Config(),
    )
    job = executor.job_manager.create_job("echo", mode="run")

    response = executor.execute(
        job.job_id,
        {
            "version": PLAN_VERSION,
            "steps": [
                {
                    "id": "echo",
                    "op": "invoke_skill",
                    "skill": "echo_skill",
                    "input": {"data": [{"id": 1}]},
                    "output": "echoed",
                },
                {"id": "save", "op": "save_result", "input": "$echoed"},
            ],
        },
    )

    assert response["success"] is True
    assert response["result"] == {"echo": [{"id": 1}]}
    assert response["metadata"]["echo_skill"]["ok"] is True
    assert response["trace"]["level2_skill_runtime_trace"][0]["runtime"] == "built_in"
