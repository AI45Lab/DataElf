from pathlib import Path

from config import Config
from runtime import JobManager
from runtime.executor import RuntimeEnvironment, RuntimeExecutor
from runtime.execution_plan import PLAN_VERSION
from runtime.skill_registry import SkillRegistry
from runtime.skill_runtime import SkillRuntime
from tools import BaseTool, ToolContext, get_global_registry


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def log_step(self, *_args, **_kwargs):
        pass


class _ListArtifactTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_artifact_tool"

    @property
    def description(self) -> str:
        return "Returns artifacts as a list."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs):
        return {
            "result": {"ok": True},
            "artifacts": [
                {"type": "file", "path": "test_data/output.json"},
            ],
            "metadata": {"count": len(kwargs.get("data", []))},
        }


def test_runtime_environment_accepts_list_artifacts_without_crashing():
    registry = get_global_registry()
    registry.clear()
    registry.register(_ListArtifactTool())

    env = RuntimeEnvironment(
        job_id="job_test",
        logger=_DummyLogger(),
    )

    result = env.run_tool("list_artifact_tool", data=[{"id": 1}])

    assert result == {"ok": True}
    assert env.artifacts["list_artifact_tool.artifacts"] == [
        {"type": "file", "path": "test_data/output.json"},
    ]
    assert env.metadata["list_artifact_tool"]["count"] == 1


def test_runtime_executor_exposes_minimal_safe_builtins_for_pipeline_repairs(tmp_path):
    cfg = Config()
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    skill_root = tmp_path / "skills" / "list_artifact_tool"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: list_artifact_tool\ndescription: Echo fixture.\n---\n\n## Usage Instructions\nEcho.",
        encoding="utf-8",
    )
    registry.register(_ListArtifactTool())
    skill_registry = SkillRegistry([tmp_path / "skills"], enabled_skills=["list_artifact_tool"])
    skill_registry.discover()
    executor = RuntimeExecutor(
        job_manager=job_manager,
        tool_registry=registry,
        skill_runtime=SkillRuntime(skill_registry, registry),
        config=cfg,
        database=None,
    )

    job = job_manager.create_job("structured execution plan", mode="pilot")
    plan = {
        "version": PLAN_VERSION,
        "steps": [
            {
                "id": "run_echo",
                "op": "invoke_skill",
                "skill": "list_artifact_tool",
                "input": {"data": [{"id": 1}]},
                "output": "result",
            },
            {"id": "save", "op": "save_result", "input": "$result"},
        ],
    }

    result = executor.execute(job.job_id, plan)

    assert result["success"] is True
    assert result["result"]["ok"] is True
