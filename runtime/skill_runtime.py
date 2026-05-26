from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm.tracing import llm_trace_context
from runtime.execution_plan import ExecutionPlanError
from runtime.skill_registry import SkillRegistry
from tools.base_tool import BaseTool, ToolContext
from tools.tool_registry import ToolRegistry


@dataclass
class SkillEnvelope:
    result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "metadata": self.metadata,
            "artifacts": self.artifacts,
            "metrics": self.metrics,
            "trace": self.trace,
        }


class BuiltInSkillRuntime:
    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry

    def invoke(self, env: Any, skill_name: str, inputs: dict[str, Any]) -> SkillEnvelope:
        backend = self.tool_registry.get(skill_name)
        if backend is None:
            raise ExecutionPlanError(f"Built-in skill backend not available: {skill_name}")

        context = _tool_context(env)
        try:
            backend.validate_parameters(**inputs)
        except ValueError as exc:
            raise ExecutionPlanError(f"Skill '{skill_name}' input error: {exc}") from exc

        try:
            with llm_trace_context(
                job_id=env.job_id,
                mode=getattr(env, "mode", None),
                scope="skill",
                caller=skill_name,
                skill_name=skill_name,
                skill_component="builtin_backend",
            ):
                output = backend.run(context, **inputs)
        except Exception as exc:
            raise ExecutionPlanError(f"Skill '{skill_name}' execution failed: {exc}") from exc

        return _normalize_backend_output(output)


class AgentSkillRuntime:
    def __init__(self, skill_registry: SkillRegistry) -> None:
        self.skill_registry = skill_registry

    def invoke(self, env: Any, skill_name: str, inputs: dict[str, Any]) -> SkillEnvelope:
        package = self.skill_registry.get(skill_name)
        if package is None:
            raise ExecutionPlanError(f"Skill not found: {skill_name}")

        manifest = self.skill_registry.manifest(skill_name)
        scripts = manifest.get("scripts", [])
        trace = {
            "skill_path": str(package.path),
            "loaded_files": ["SKILL.md"],
            "manifest": manifest,
        }
        if not scripts:
            raise ExecutionPlanError(
                f"External skill '{skill_name}' has no runnable script. "
                "Provide a scripts/run.py entrypoint or use a built-in skill binding."
            )

        run_script = package.path / "scripts" / "run.py"
        if not run_script.exists():
            raise ExecutionPlanError(f"External skill '{skill_name}' must provide scripts/run.py.")

        import json

        process = subprocess.run(
            [sys.executable, str(run_script)],
            input=json.dumps(inputs, ensure_ascii=False),
            capture_output=True,
            text=True,
            cwd=str(package.path),
            timeout=300,
            check=False,
        )
        trace["script"] = "scripts/run.py"
        trace["stdout"] = process.stdout
        trace["stderr"] = process.stderr
        trace["returncode"] = process.returncode
        if process.returncode != 0:
            raise ExecutionPlanError(
                f"External skill '{skill_name}' failed with exit code {process.returncode}: {process.stderr}"
            )
        try:
            output = json.loads(process.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ExecutionPlanError(f"External skill '{skill_name}' did not return JSON.") from exc

        envelope = _normalize_backend_output(output)
        envelope.trace.update(trace)
        return envelope


class SkillRuntime:
    def __init__(
        self,
        skill_registry: SkillRegistry,
        tool_registry: ToolRegistry,
        builtin_bindings: dict[str, BaseTool] | None = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry
        for name, backend in (builtin_bindings or {}).items():
            if self.tool_registry.get(name) is None:
                self.tool_registry.register(backend)
        self._builtins = BuiltInSkillRuntime(tool_registry)
        self._external = AgentSkillRuntime(skill_registry)

    def invoke(self, env: Any, skill_name: str, inputs: dict[str, Any]) -> SkillEnvelope:
        package = self.skill_registry.get(skill_name)
        if package is None:
            raise ExecutionPlanError(f"Skill not found: {skill_name}")
        if self.tool_registry.get(skill_name) is not None:
            envelope = self._builtins.invoke(env, skill_name, inputs)
            envelope.trace.setdefault("runtime", "built_in")
            envelope.trace.setdefault("skill_name", skill_name)
            envelope.trace.setdefault("loaded_files", ["SKILL.md"])
            return envelope
        envelope = self._external.invoke(env, skill_name, inputs)
        envelope.trace.setdefault("runtime", "agent_skill")
        envelope.trace.setdefault("skill_name", skill_name)
        return envelope


def _tool_context(env: Any) -> ToolContext:
    effective_llm = env.tool_llm if env.tool_llm else env.llm
    if not env.tool_llm:
        env.logger.warning(
            "Tool LLM not configured, falling back to Agent LLM. "
            "Configure tool_llm in config for independent LLM settings."
        )
    return ToolContext(
        job_id=env.job_id,
        logger=env.logger,
        mode=env.mode,
        config=env.config.__dict__ if env.config else {},
        llm=effective_llm,
        datasets=env._datasets.copy(),
        artifacts=env._artifacts.copy(),
        metadata=env._metadata.copy(),
    )


def _normalize_backend_output(output: Any) -> SkillEnvelope:
    if not isinstance(output, dict):
        return SkillEnvelope(result=output)

    if any(key in output for key in ("result", "metadata", "artifacts", "metrics", "trace")):
        return SkillEnvelope(
            result=output.get("result"),
            metadata=output.get("metadata", {}) if isinstance(output.get("metadata", {}), dict) else {},
            artifacts=output.get("artifacts", {}) if isinstance(output.get("artifacts", {}), dict) else {},
            metrics=output.get("metrics", {}) if isinstance(output.get("metrics", {}), dict) else {},
            trace=output.get("trace", {}) if isinstance(output.get("trace", {}), dict) else {},
        )
    return SkillEnvelope(result=output)
