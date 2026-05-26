from __future__ import annotations

import logging
import json
import sys
import traceback
from io import StringIO
from pathlib import Path
from typing import Any

from config import Config
from database import DatabaseStrategy
from llm.tracing import llm_trace_context
from runtime.execution_plan import ExecutionPlanError, parse_execution_plan, resolve_value, validate_execution_plan
from runtime.job_manager import Job, JobManager, JobStatus
from runtime.skill_runtime import SkillRuntime
from tools import ToolRegistry
from exceptions import (
    DatasetNotFoundError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolParameterError,
    PipelineExecutionError,
)


class _ExternalToolLogHandler(logging.Handler):
    def __init__(self, job_logger: Any) -> None:
        super().__init__(level=logging.WARNING)
        self.job_logger = job_logger

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("pilot."):
            return
        try:
            message = f"{record.name}: {record.getMessage()}"
            if record.levelno >= logging.ERROR:
                self.job_logger.error(message, source_logger=record.name)
            else:
                self.job_logger.warning(message, source_logger=record.name)
        except Exception:
            return


class RuntimeEnvironment:

    def __init__(
        self,
        job_id: str,
        logger: Any,
        database: DatabaseStrategy | None = None,
        config: Config | None = None,
        llm: Any = None,
        tool_llm: Any = None,
    ):

        self.job_id = job_id
        self.logger = logger
        self.database = database
        self.config = config
        self.llm = llm  # LLM for agent pipeline generation
        self.tool_llm = tool_llm  # LLM for tools that need LLM
        self.mode = "unknown"
        self._result: Any = None
        self._artifacts: dict[str, Any] = {}
        self._metadata: dict[str, Any] = {}
        self._datasets: dict[str, Any] = {}
        self._trace: dict[str, Any] = {
            "level1_plan_trace": {},
            "level2_skill_runtime_trace": [],
            "level3_skill_internal_trace": [],
        }

    def load_dataset(
        self,
        name: str,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        columns: list[str] | None = None,
    ) -> Any:
        #Load dataset from configured database.
        self.logger.log_step(f"Loading dataset: {name}")

        if not self.database:
            raise DatasetNotFoundError(
                f"Cannot load dataset '{name}': No database configured. "
                "Please check your config.yaml database settings."
            )

        try:
            data = self.database.read_table(
                table_name=name,
                filters=filters,
                limit=limit,
                columns=columns,
            )
            if data:
                self._datasets[name] = data
                return data
        except Exception as e:
            raise DatasetNotFoundError(
                f"Failed to load dataset '{name}' from database: {e}"
            )

        raise DatasetNotFoundError(
            f"Dataset '{name}' not found in database. "
            "Please check the table name and database configuration."
        )

    def run_tool(self, tool_name: str, **kwargs: Any) -> Any:
        from tools import get_global_registry

        self.logger.log_step(f"Running tool: {tool_name}")

        registry = get_global_registry()
        tool = registry.get(tool_name)

        if tool is None:
            raise ToolNotFoundError(
                f"Tool '{tool_name}' not found. "
                f"Available internal backends: {', '.join(registry.list_tools())}"
            )

        # Create context - use tool_llm if available, otherwise use agent's llm
        from tools.base_tool import ToolContext

        # Tool uses tool_llm if configured, otherwise falls back to agent's llm
        effective_llm = self.tool_llm if self.tool_llm else self.llm
        if not self.tool_llm:
            self.logger.warning(
                f"Tool LLM not configured, falling back to Agent LLM. "
                f"Tool calls will use Agent model. "
                f"Configure tool_llm in config for independent LLM settings."
            )

        context = ToolContext(
            job_id=self.job_id,
            logger=self.logger,
            mode=self.mode,
            config=self.config.__dict__ if self.config else {},
            llm=effective_llm,
            datasets=self._datasets.copy(),
            artifacts=self._artifacts.copy(),
            metadata=self._metadata.copy(),
        )

        # Validate parameters
        try:
            tool.validate_parameters(**kwargs)
        except ValueError as e:
            raise ToolParameterError(f"Tool '{tool_name}' parameter error: {e}")

        # Run tool
        try:
            with llm_trace_context(
                job_id=self.job_id,
                mode=getattr(self, "mode", None),
                scope="tool",
                caller=tool_name,
                tool_name=tool_name,
            ):
                tool_output = tool.run(context, **kwargs)
        except Exception as e:
            raise ToolExecutionError(f"Tool '{tool_name}' execution failed: {e}")

        # Store artifacts and metadata
        if isinstance(tool_output, dict):
            if "artifacts" in tool_output:
                tool_artifacts = tool_output["artifacts"]
                if isinstance(tool_artifacts, dict):
                    # Prefix with tool name to avoid collisions
                    for key, value in tool_artifacts.items():
                        self._artifacts[f"{tool_name}.{key}"] = value
                else:
                    # Some derived/experimental tools return artifact lists rather than keyed dicts.
                    # Preserve them without crashing the runtime.
                    self._artifacts[f"{tool_name}.artifacts"] = tool_artifacts
            if "metadata" in tool_output:
                self._metadata[tool_name] = tool_output["metadata"]

        # Return just the result field
        return tool_output.get("result") if isinstance(tool_output, dict) else tool_output

    def invoke_skill(self, skill_runtime: SkillRuntime, skill_name: str, **kwargs: Any) -> Any:
        self.logger.log_step(f"Invoking skill: {skill_name}")
        envelope = skill_runtime.invoke(self, skill_name, kwargs)

        for key, value in envelope.artifacts.items():
            self._artifacts[f"{skill_name}.{key}"] = value
        if envelope.metadata:
            self._metadata[skill_name] = envelope.metadata
        if envelope.metrics:
            self._metadata[f"{skill_name}.metrics"] = envelope.metrics
        if envelope.trace:
            self._trace["level2_skill_runtime_trace"].append(envelope.trace)
            if envelope.trace.get("stdout") or envelope.trace.get("stderr") or envelope.trace.get("metrics"):
                self._trace["level3_skill_internal_trace"].append(envelope.trace)
        return envelope.result

    def save_result(self, result: Any) -> None:
        # Save result to job result. Accessible via pilot result.
        self._result = result
        self.logger.log_step("Saving result to job")

    def write_file(self, data: Any, path: str) -> None:
        # Write data to a local JSON file.
        import json
        from pathlib import Path

        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        self.logger.log_step(f"Writing data to file: {path}")

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.logger.log_step(f"Successfully wrote {len(data)} records to {path}")
        except Exception as e:
            self.logger.error(f"Failed to write file: {e}")
            raise

    def write_db(self, data: Any, table: str) -> None:
        # Write data to configured database. As configured in config yml.
        if not self.database:
            raise ValueError("No database configured. Please check config.yaml database settings.")

        self.logger.log_step(f"Writing data to database table: {table}")

        # Convert to list if needed
        if isinstance(data, dict):
            data = [data]

        try:
            self.database.write_table(table_name=table, data=data)
            self.logger.log_step(f"Successfully wrote {len(data)} records to table '{table}'")
        except Exception as e:
            self.logger.error(f"Failed to write to database: {e}")
            raise

    def log_step(self, message: str) -> None:
        self.logger.log_step(message)

    @property
    def result(self) -> Any:
        return self._result

    @property
    def artifacts(self) -> dict[str, Any]:
        return self._artifacts.copy()

    @property
    def metadata(self) -> dict[str, Any]:
        return self._metadata.copy()

    @property
    def trace(self) -> dict[str, Any]:
        return {
            "level1_plan_trace": dict(self._trace.get("level1_plan_trace", {})),
            "level2_skill_runtime_trace": list(self._trace.get("level2_skill_runtime_trace", [])),
            "level3_skill_internal_trace": list(self._trace.get("level3_skill_internal_trace", [])),
        }


class RuntimeExecutor:
    def __init__(
        self,
        job_manager: JobManager,
        tool_registry: ToolRegistry,
        config: Config,
        skill_runtime: SkillRuntime | None = None,
        database: DatabaseStrategy | None = None,
        llm_provider: Any = None,
        tool_llm_provider: Any = None,
    ):

        self.job_manager = job_manager
        self.tool_registry = tool_registry
        if skill_runtime is None:
            from runtime.skill_registry import SkillRegistry, builtin_skill_root

            skill_registry = SkillRegistry([builtin_skill_root()])
            skill_registry.discover()
            skill_runtime = SkillRuntime(skill_registry=skill_registry, tool_registry=tool_registry)
        self.skill_runtime = skill_runtime
        self.config = config
        self.database = database
        self.llm_provider = llm_provider
        self.tool_llm_provider = tool_llm_provider

    def execute(self, job_id: str, execution_plan: str | dict[str, Any]) -> dict[str, Any]:
        job = self.job_manager.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        # Update status to running
        self.job_manager.update_status(job_id, JobStatus.RUNNING)

        # Setup logger
        import pilog
        logger = pilog.get_logger(job_id, self.config, self.database)
        external_log_handler = _ExternalToolLogHandler(logger)
        tools_logger = logging.getLogger("tools")
        tools_logger.addHandler(external_log_handler)

        # Create runtime environment
        env = RuntimeEnvironment(
            job_id=job_id,
            logger=logger,
            database=self.database,
            config=self.config,
            llm=self.llm_provider,
            tool_llm=self.tool_llm_provider,
        )
        env.mode = job.mode

        # Capture output
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        execution_response: dict[str, Any] | None = None

        plan_text = execution_plan if isinstance(execution_plan, str) else json.dumps(execution_plan, ensure_ascii=False, indent=2)

        try:
            plan = parse_execution_plan(execution_plan)
            validate_execution_plan(
                plan.to_dict(),
                available_skills=set(self.skill_runtime.skill_registry.list_names()),
            )
            variables: dict[str, Any] = {}
            env._trace["level1_plan_trace"] = {
                "execution_plan": plan.to_dict(),
                "dataset_refs": [],
                "selected_skills": [],
                "result_refs": [],
                "policy_decisions": [],
            }

            for step in plan.steps:
                raw_step = step.raw
                op = step.op
                if op == "load_dataset":
                    data = env.load_dataset(
                        raw_step["dataset"],
                        filters=resolve_value(raw_step.get("filters"), variables),
                        limit=resolve_value(raw_step.get("limit"), variables),
                        columns=resolve_value(raw_step.get("columns"), variables),
                    )
                    variables[raw_step["output"]] = data
                    env._trace["level1_plan_trace"]["dataset_refs"].append(raw_step["dataset"])
                elif op == "invoke_skill":
                    inputs = resolve_value(raw_step.get("input", {}), variables)
                    result_value = env.invoke_skill(
                        self.skill_runtime,
                        raw_step["skill"],
                        **inputs,
                    )
                    variables[raw_step["output"]] = result_value
                    env._trace["level1_plan_trace"]["selected_skills"].append(raw_step["skill"])
                elif op == "save_result":
                    value = resolve_value(raw_step["input"], variables)
                    env.save_result(value)
                    env._trace["level1_plan_trace"]["result_refs"].append(raw_step["id"])
                elif op == "write_file":
                    value = resolve_value(raw_step["input"], variables)
                    env.write_file(value, raw_step["path"])
                elif op == "write_db":
                    value = resolve_value(raw_step["input"], variables)
                    env.write_db(value, raw_step["table"])
                elif op == "log":
                    env.log_step(raw_step["message"])

            # Get result and artifacts
            result = env.result
            artifacts = env.artifacts
            metadata = env.metadata
            trace = env.trace

            # Update job with full output including artifacts
            output = {"result": result}
            if artifacts:
                output["artifacts"] = artifacts
            if metadata:
                output["metadata"] = metadata
            output["trace"] = trace

            self.job_manager.update_result(job_id, output)
            self.job_manager.update_status(job_id, JobStatus.COMPLETED)

            execution_response = {
                "success": True,
                "result": result,
                "artifacts": artifacts,
                "metadata": metadata,
                "trace": trace,
                "error": None,
            }

        except (DatasetNotFoundError, ToolNotFoundError, ToolParameterError, ToolExecutionError, ExecutionPlanError) as e:
            # Known pilot errors - provide clean error message
            error_msg = str(e)
            logger.error(f"Pipeline execution failed: {error_msg}")
            self.job_manager.update_error(job_id, error_msg)

            execution_response = {
                "success": False,
                "result": None,
                "artifacts": {},
                "metadata": {},
                "trace": env.trace,
                "error": error_msg,
            }

        except Exception as e:
            # Unknown errors - include full traceback for debugging
            error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
            logger.error(f"Pipeline execution failed: {error_msg}")
            self.job_manager.update_error(job_id, error_msg)

            execution_response = {
                "success": False,
                "result": None,
                "artifacts": {},
                "metadata": {},
                "trace": env.trace,
                "error": error_msg,
            }

        finally:
            # Restore stdout first
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            tools_logger.removeHandler(external_log_handler)

            # Mark job completion and record last step duration
            logger.finish()

            # Save structured execution plan to file for reference
            pipeline_dir = Path("pipelines")
            pipeline_dir.mkdir(exist_ok=True)
            pipeline_file = pipeline_dir / f"{job_id}.plan.json"
            with open(pipeline_file, "w") as f:
                f.write(plan_text)

        if execution_response is None:
            execution_response = {
                "success": False,
                "result": None,
                "artifacts": {},
                "metadata": {},
                "trace": env.trace,
                "error": "Pipeline execution did not produce a response.",
            }
        execution_response.update(_execution_log_context(logger))
        return execution_response


def _execution_log_context(logger: Any) -> dict[str, Any]:
    logs = list(getattr(logger, "entries", []))
    return {
        "log_ref": getattr(logger, "log_ref", None),
        "logs": logs,
        "log_excerpt": _select_log_excerpt(logs),
    }


def _select_log_excerpt(logs: list[dict[str, Any]], max_entries: int = 8) -> list[dict[str, Any]]:
    important_levels = {"WARNING", "ERROR", "CRITICAL"}
    important = [entry for entry in logs if entry.get("level") in important_levels]
    selected = important[-max_entries:] if important else logs[-min(max_entries, 5):]
    excerpt: list[dict[str, Any]] = []
    for entry in selected:
        excerpt.append({
            "step": entry.get("step"),
            "level": entry.get("level"),
            "message": entry.get("message"),
            "timestamp": entry.get("timestamp"),
            "duration_ms": entry.get("duration_ms"),
        })
    return excerpt
