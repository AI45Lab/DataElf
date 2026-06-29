from __future__ import annotations

import threading
from dataclasses import dataclass
from inspect import signature
from typing import Any, Callable

from agentic.controller import PilotController, RunCoordinator
from runtime import JobManager, JobStatus

from .checkpoints import WebCheckpointBroker
from .command_parser import parse_user_command
from .catalog import dataset_catalog_with_columns
from .events import JobEventBus
from .run_preflight import (
    append_tool_to_task,
    append_dataset_to_task,
    build_dataset_selection_payload,
    build_tool_selection_payload,
    needs_dataset_selection,
    needs_tool_selection,
    resolve_dataset_answer,
    resolve_tool_answer,
)
from .sessions import WebSessionManager


@dataclass(frozen=True)
class RunSubmission:
    command: str
    session_id: str | None = None


@dataclass(frozen=True)
class RunSubmissionResponse:
    job_id: str
    mode: str
    status: str
    task: str
    stream_url: str


CoordinatorFactory = Callable[[Any, WebCheckpointBroker], Any]
PilotControllerFactory = Callable[[Any, WebCheckpointBroker], Any]


class RunWebService:
    def __init__(
        self,
        *,
        environment: Any,
        event_bus: JobEventBus | None = None,
        checkpoint_broker: WebCheckpointBroker | None = None,
        coordinator_factory: CoordinatorFactory | None = None,
        pilot_controller_factory: PilotControllerFactory | None = None,
        run_in_background: bool = True,
        session_manager: WebSessionManager | None = None,
    ) -> None:
        self.environment = environment
        self.event_bus = event_bus or JobEventBus()
        self.checkpoint_broker = checkpoint_broker or WebCheckpointBroker()
        self.coordinator_factory = coordinator_factory or _default_coordinator_factory
        self.pilot_controller_factory = pilot_controller_factory or _default_pilot_controller_factory
        self.run_in_background = run_in_background
        self.session_manager = session_manager or WebSessionManager()
        self._published_pipeline_job_ids: set[str] = set()
        self._pipeline_llm_metadata_by_job: dict[str, dict[str, Any]] = {}
        self._published_log_keys_by_job: dict[str, set[tuple[str, str, str, str, str]]] = {}
        self._active_pilot_attempt_by_job: dict[str, str] = {}

    def submit_run(self, submission: RunSubmission) -> RunSubmissionResponse:
        parsed = parse_user_command(submission.command)
        if parsed.mode != "run":
            return RunSubmissionResponse(
                job_id="",
                mode=parsed.mode,
                status="unsupported",
                task=parsed.task,
                stream_url="",
            )

        job_manager = _env_get(self.environment, "job_manager")
        job = job_manager.create_job(parsed.task, mode="run")
        self.event_bus.publish(
            job.job_id,
            {
                "type": "job.created",
                "mode": "run",
                "status": job.status.value,
                "task": parsed.task,
                "raw_command": parsed.raw_command,
                "session_id": submission.session_id,
            },
        )

        if self.run_in_background:
            thread = threading.Thread(
                target=self._execute_run,
                args=(job.job_id, parsed.task),
                daemon=True,
            )
            thread.start()
            status = job.status.value
        else:
            result = self._execute_run(job.job_id, parsed.task)
            status = str(result.get("status", job_manager.get_job(job.job_id).status.value))

        return RunSubmissionResponse(
            job_id=job.job_id,
            mode="run",
            status=status,
            task=parsed.task,
            stream_url=f"/api/v1/jobs/{job.job_id}/events",
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job_manager = _env_get(self.environment, "job_manager")
        job = job_manager.get_job(job_id)
        if job is None:
            return None
        payload = job.to_dict()
        if payload.get("mode") == "pilot":
            payload = self._hydrate_pilot_job_payload(payload)
        return payload

    def create_session(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        return self.session_manager.create_session(name=payload.get("name"))

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.session_manager.list_sessions()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.session_manager.get_session(session_id)

    def update_session(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.session_manager.update_session(session_id, name=payload.get("name"))

    def delete_session(self, session_id: str) -> bool:
        return self.session_manager.delete_session(session_id)

    def set_session_mode(self, session_id: str, mode: str) -> dict[str, Any] | None:
        return self.session_manager.set_mode(session_id, mode)

    def save_session_snapshot(
        self,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.session_manager.update_snapshot(session_id, snapshot)

    def submit_session_run(
        self,
        session_id: str,
        *,
        command: str,
        budget_steps: int | None = None,
    ) -> RunSubmissionResponse:
        session = self.session_manager.get_session(session_id)
        if session is None:
            raise KeyError("session not found")
        if session.get("locked"):
            raise RuntimeError("Session is locked because its job already finished.")
        if session.get("job_id"):
            raise RuntimeError("Session already has a job.")
        if not session.get("mode"):
            raise RuntimeError("Session mode must be selected before submitting a run.")

        if session.get("backend_mode") == "pilot":
            return self._submit_session_pilot(
                session_id,
                command=command,
                budget_steps=budget_steps,
            )
        if session.get("backend_mode") != "run":
            raise RuntimeError("Session backend mode is not supported.")

        parsed = parse_user_command(self._command_for_session_backend(command, session))
        if parsed.mode != "run":
            raise RuntimeError("Session backend mode is not supported.")

        job_manager = _env_get(self.environment, "job_manager")
        job = job_manager.create_job(parsed.task, mode="run")
        self.session_manager.bind_job(session_id, job.job_id, status="running")
        self.event_bus.publish(
            job.job_id,
            {
                "type": "job.created",
                "mode": "run",
                "status": job.status.value,
                "task": parsed.task,
                "raw_command": parsed.raw_command,
                "session_id": session_id,
            },
        )

        if self.run_in_background:
            thread = threading.Thread(
                target=self._execute_run,
                args=(job.job_id, parsed.task),
                daemon=True,
            )
            thread.start()
            status = "running"
        else:
            result = self._execute_run(job.job_id, parsed.task)
            status = str(result.get("status", job_manager.get_job(job.job_id).status.value))

        return RunSubmissionResponse(
            job_id=job.job_id,
            mode="run",
            status=status,
            task=parsed.task,
            stream_url=f"/api/v1/jobs/{job.job_id}/events",
        )

    def _submit_session_pilot(
        self,
        session_id: str,
        *,
        command: str,
        budget_steps: int | None = None,
    ) -> RunSubmissionResponse:
        parsed = parse_user_command(command)
        task = parsed.task or parsed.raw_command
        if not task:
            raise RuntimeError("Pilot task is required.")

        job_manager = _env_get(self.environment, "job_manager")
        job = job_manager.create_job(task, mode="pilot")
        self.session_manager.bind_job(session_id, job.job_id, status="running")
        self.event_bus.publish(
            job.job_id,
            {
                "type": "job.created",
                "mode": "pilot",
                "status": job.status.value,
                "task": task,
                "raw_command": parsed.raw_command,
                "session_id": session_id,
            },
        )

        if self.run_in_background:
            thread = threading.Thread(
                target=self._execute_pilot,
                args=(job.job_id, task, budget_steps or 3),
                daemon=True,
            )
            thread.start()
            status = "running"
        else:
            result = self._execute_pilot(job.job_id, task, budget_steps or 3)
            status = _pilot_response_status(result.get("status", job_manager.get_job(job.job_id).status.value))

        return RunSubmissionResponse(
            job_id=job.job_id,
            mode="pilot",
            status=status,
            task=task,
            stream_url=f"/api/v1/jobs/{job.job_id}/events",
        )

    def answer_checkpoint(
        self,
        *,
        job_id: str,
        checkpoint_id: str,
        answer: dict[str, Any],
    ) -> bool:
        accepted = self.checkpoint_broker.answer_checkpoint(
            job_id=job_id,
            checkpoint_id=checkpoint_id,
            answer=answer,
        )
        if accepted:
            self.event_bus.publish(
                job_id,
                {
                    "type": "checkpoint.answered",
                    "checkpoint_id": checkpoint_id,
                    "answer": answer,
                },
            )
        return accepted

    def list_datasets(self) -> list[dict[str, Any]]:
        dataset_schemas = _env_get(self.environment, "dataset_schemas", {})
        return dataset_catalog_with_columns(dataset_schemas)

    def list_tools(self) -> list[dict[str, Any]]:
        registry = _env_get(self.environment, "registry")
        return list(registry.list_schemas())

    def _execute_run(self, job_id: str, task: str) -> dict[str, Any]:
        job_manager: JobManager = _env_get(self.environment, "job_manager")
        try:
            job_manager.update_status(job_id, JobStatus.RUNNING)
            self.event_bus.publish(job_id, {"type": "job.running", "status": "running"})
            dataset_schemas = _env_get(self.environment, "dataset_schemas", {})
            task = self._resolve_dataset_if_needed(job_id, task, dataset_schemas)
            registry = _env_get(self.environment, "registry")
            task = self._resolve_tool_if_needed(job_id, task, list(registry.list_schemas()))
            run_environment = _environment_with_executor(
                self.environment,
                _RealtimeLogExecutor(
                    _env_get(self.environment, "executor"),
                    lambda log: self._publish_runtime_log(job_id, log),
                ),
            )
            coordinator = self.coordinator_factory(run_environment, self.checkpoint_broker)
            result = coordinator.execute(
                job_id=job_id,
                task=task,
                dataset_schemas=dataset_schemas,
                ask_user=True,
                verbose=False,
                event_handler=lambda event: self._publish_backend_event(job_id, event),
                checkpoint_handler=lambda payload: self._wait_for_checkpoint_answer(job_id, payload),
            )

            pipeline = result.get("pipeline", "")
            if pipeline:
                self._publish_pipeline_generated_if_available(
                    job_id,
                    pipeline=pipeline,
                    llm_metadata=result.get("llm_metadata", {}),
                )
            execution = result.get("execution", {})
            if isinstance(execution, dict):
                self._persist_execution_log_context(job_id, execution)
            for log in execution.get("logs", []) if isinstance(execution, dict) else []:
                self._publish_runtime_log(job_id, log)
            if result.get("status") == "completed":
                job_manager.update_status(job_id, JobStatus.COMPLETED)
                final_type = "job.completed"
                self.session_manager.complete_for_job(job_id, "completed")
            else:
                error = execution.get("error") if isinstance(execution, dict) else None
                job_manager.update_error(job_id, error or "Run failed.")
                final_type = "job.failed"
                self.session_manager.complete_for_job(job_id, "failed")
            self.event_bus.publish(
                job_id,
                {
                    "type": final_type,
                    "status": result.get("status", "failed"),
                    "result": execution.get("result") if isinstance(execution, dict) else None,
                    "execution": execution,
                    "clarification": result.get("clarification", {}),
                    "capability_gap": result.get("capability_gap", {}),
                    "error": execution.get("error") if isinstance(execution, dict) else None,
                },
            )
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            job_manager.update_error(job_id, error)
            self.event_bus.publish(
                job_id,
                {
                    "type": "job.failed",
                    "status": "failed",
                    "error": error,
                },
            )
            self.session_manager.complete_for_job(job_id, "failed")
            return {"job_id": job_id, "status": "failed", "execution": {"error": error}}

    def _execute_pilot(self, job_id: str, task: str, budget_steps: int = 3) -> dict[str, Any]:
        job_manager: JobManager = _env_get(self.environment, "job_manager")
        try:
            job_manager.update_status(job_id, JobStatus.RUNNING)
            self.event_bus.publish(job_id, {"type": "job.running", "mode": "pilot", "status": "running"})
            dataset_schemas = _env_get(self.environment, "dataset_schemas", {})
            task = self._resolve_dataset_if_needed(job_id, task, dataset_schemas)
            registry = _env_get(self.environment, "registry")
            task = self._resolve_tool_if_needed(job_id, task, list(registry.list_schemas()))
            pilot_environment = _environment_with_executor(
                self.environment,
                _RealtimeLogExecutor(
                    _env_get(self.environment, "executor"),
                    lambda log: self._publish_pilot_runtime_log(job_id, log),
                ),
            )
            controller = self.pilot_controller_factory(pilot_environment, self.checkpoint_broker)
            result = controller.execute(
                job_id=job_id,
                task=task,
                dataset_schemas=dataset_schemas,
                budget_steps=budget_steps,
                allow_experimental_tools=False,
                ask_user=True,
                event_handler=lambda event: self._publish_pilot_backend_event(job_id, event),
                checkpoint_handler=lambda payload: self._wait_for_checkpoint_answer(job_id, payload),
            )

            persisted_job = job_manager.get_job(job_id)
            persisted_result = persisted_job.result if persisted_job is not None else {}
            final_payload = self._pilot_completion_payload(job_id, result, persisted_result)
            if result.get("status") == "success":
                if persisted_job is None or persisted_job.status != JobStatus.COMPLETED:
                    job_manager.update_status(job_id, JobStatus.COMPLETED)
                final_type = "job.completed"
                self.session_manager.complete_for_job(job_id, "completed")
            else:
                if persisted_job is None or persisted_job.status != JobStatus.FAILED:
                    job_manager.update_error(
                        job_id,
                        str(result.get("error") or result.get("status") or "Pilot failed."),
                    )
                final_type = "job.failed"
                self.session_manager.complete_for_job(job_id, "failed")
            self.event_bus.publish(job_id, {"type": final_type, **final_payload})
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            job_manager.update_error(job_id, error)
            self.event_bus.publish(
                job_id,
                {
                    "type": "job.failed",
                    "mode": "pilot",
                    "status": "failed",
                    "error": error,
                },
            )
            self.session_manager.complete_for_job(job_id, "failed")
            return {"job_id": job_id, "status": "failed", "execution": {"error": error}}
        finally:
            self._active_pilot_attempt_by_job.pop(job_id, None)

    def _pilot_completion_payload(
        self,
        job_id: str,
        result: dict[str, Any],
        persisted_result: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = persisted_result.get("metadata", {}) if isinstance(persisted_result, dict) else {}
        execution = {
            "result": persisted_result.get("result") if isinstance(persisted_result, dict) else None,
            "artifacts": persisted_result.get("artifacts", {}) if isinstance(persisted_result, dict) else {},
            "metadata": metadata,
            "logs": persisted_result.get("logs", []) if isinstance(persisted_result, dict) else [],
            "log_ref": persisted_result.get("log_ref") if isinstance(persisted_result, dict) else None,
            "log_excerpt": persisted_result.get("log_excerpt", []) if isinstance(persisted_result, dict) else [],
            "error": None,
        }
        best_attempt = result.get("best_attempt") or {}
        judge = best_attempt.get("judge", {}) if isinstance(best_attempt, dict) else {}
        job_manager: JobManager = _env_get(self.environment, "job_manager")
        job = job_manager.get_job(job_id)
        return {
            "mode": "pilot",
            "status": result.get("status", "failed"),
            "result": execution["result"],
            "execution": execution,
            "attempts": result.get("attempts", []),
            "best_attempt": best_attempt,
            "judge": judge,
            "pilot_summary": result.get("pilot_summary", {}),
            "pipeline_candidate_id": result.get("pipeline_candidate_id"),
            "approved_asset_ids": result.get("approved_asset_ids", []),
            "candidate_asset_ids": job.candidate_asset_ids if job is not None else [],
            "clarification": result.get("goal_clarification", {}),
            "error": execution["error"],
        }

    def _hydrate_pilot_job_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        asset_manager = _env_get(self.environment, "asset_manager", None)
        if asset_manager is None or not hasattr(asset_manager, "list_attempts"):
            return payload

        attempts = asset_manager.list_attempts(str(payload.get("job_id", "")))
        if not attempts:
            return {**payload, "attempts": []}

        best_attempt = max(
            attempts,
            key=lambda attempt: float(attempt.get("judge", {}).get("score", 0.0) or 0.0),
        )
        result = payload.get("result", {}) if isinstance(payload.get("result"), dict) else {}
        metadata = result.get("metadata", {}) if isinstance(result.get("metadata"), dict) else {}
        pilot_summary = metadata.get("pilot_summary")
        if not isinstance(pilot_summary, dict) or not pilot_summary:
            pilot_summary = _build_pilot_summary_from_attempts(attempts)

        return {
            **payload,
            "attempts": attempts,
            "best_attempt": best_attempt,
            "pilot_summary": pilot_summary,
        }

    def _command_for_session_backend(
        self,
        command: str,
        session: dict[str, Any],
    ) -> str:
        if session.get("backend_mode") != "run":
            return command
        parsed = parse_user_command(command)
        if parsed.mode == "run":
            return command
        escaped_task = parsed.task.replace("\\", "\\\\").replace('"', '\\"')
        return f'elf run "{escaped_task}"'

    def _resolve_dataset_if_needed(
        self,
        job_id: str,
        task: str,
        dataset_schemas: dict[str, list[str]],
    ) -> str:
        if not needs_dataset_selection(task, dataset_schemas):
            return task

        response = self._wait_for_checkpoint_answer(
            job_id,
            build_dataset_selection_payload(dataset_schemas),
        )
        dataset_name = resolve_dataset_answer(
            str(response.get("answer", "")),
            dataset_schemas,
        )
        return append_dataset_to_task(task, dataset_name)

    def _resolve_tool_if_needed(
        self,
        job_id: str,
        task: str,
        tool_schemas: list[dict[str, Any]],
    ) -> str:
        if not needs_tool_selection(task, tool_schemas):
            return task

        response = self._wait_for_checkpoint_answer(
            job_id,
            build_tool_selection_payload(tool_schemas),
        )
        tool_name = resolve_tool_answer(
            str(response.get("answer", "")),
            tool_schemas,
        )
        return append_tool_to_task(task, tool_name)

    def _wait_for_checkpoint_answer(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job_manager: JobManager = _env_get(self.environment, "job_manager")
        checkpoint_type = str(payload.get("checkpoint_type", "clarification"))
        checkpoint_payload = dict(payload.get("payload", payload))
        checkpoint = self.checkpoint_broker.create_checkpoint(
            job_id=job_id,
            checkpoint_type=checkpoint_type,
            payload=checkpoint_payload,
        )
        persisted_checkpoint_payload = {
            **checkpoint_payload,
            "checkpoint_id": checkpoint.checkpoint_id,
        }
        job_manager.update_checkpoint(
            job_id,
            checkpoint_type=checkpoint_type,
            checkpoint_state="pending",
            checkpoint_payload=persisted_checkpoint_payload,
            status=JobStatus.PAUSED,
        )
        self.event_bus.publish(
            job_id,
            {
                "type": "checkpoint.created",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "payload": checkpoint.payload,
            },
        )
        response = self.checkpoint_broker.wait_for_answer(
            job_id=job_id,
            checkpoint_id=checkpoint.checkpoint_id,
        )
        job_manager.update_checkpoint(
            job_id,
            checkpoint_type=checkpoint_type,
            checkpoint_state="resolved",
            checkpoint_payload=checkpoint_payload,
            status=JobStatus.RUNNING,
        )
        self.event_bus.publish(
            job_id,
            {
                "type": "checkpoint.resolved",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "response": response,
            },
        )
        return response

    def _publish_backend_event(self, job_id: str, event: dict[str, Any]) -> None:
        event_type = event.get("type", "backend.event")
        if (
            event_type == "stage_completed"
            and event.get("stage") == "pipeline_generation"
            and isinstance(event.get("llm"), dict)
        ):
            self._pipeline_llm_metadata_by_job[job_id] = dict(event["llm"])
        if event_type == "stage_started" and event.get("stage") == "execution":
            self._publish_pipeline_generated_if_available(job_id)
            self._publish_runtime_log(
                job_id,
                {
                    "source": "stage",
                    "level": "INFO",
                    "message": "Execution: Running pipeline",
                },
            )
        if event_type == "stage_completed" and event.get("stage") == "execution":
            success = bool(event.get("success"))
            self._publish_runtime_log(
                job_id,
                {
                    "source": "stage",
                    "level": "SUCCESS" if success else "ERROR",
                    "message": f"Execution: status={'success' if success else 'failed'}",
                    "icon": "✅" if success else "❌",
                },
            )
        self.event_bus.publish(job_id, {"type": f"backend.{event_type}", "backend_event": event})

    def _publish_pilot_backend_event(self, job_id: str, event: dict[str, Any]) -> None:
        event_job_id = str(event.get("job_id") or job_id)
        event_type = event.get("type", "backend.event")
        attempt_id = event.get("attempt_id")
        if attempt_id:
            self._active_pilot_attempt_by_job[event_job_id] = str(attempt_id)

        if event_type == "stage_started" and event.get("stage") == "execution":
            self._publish_pilot_runtime_log(
                event_job_id,
                {
                    "source": "stage",
                    "level": "INFO",
                    "message": "Execution: Running pipeline",
                    "attempt_id": attempt_id,
                },
            )
        if event_type == "stage_completed" and event.get("stage") == "execution":
            success = bool(event.get("success"))
            self._publish_pilot_runtime_log(
                event_job_id,
                {
                    "source": "stage",
                    "level": "SUCCESS" if success else "ERROR",
                    "message": f"Execution: status={'success' if success else 'failed'}",
                    "icon": "✅" if success else "❌",
                    "attempt_id": attempt_id,
                },
            )

        pilot_event_types = {
            "attempt_started",
            "planner",
            "pipeline",
            "judge",
            "candidate_saved",
            "candidate_validated",
            "candidate_error",
        }
        if event_type in pilot_event_types:
            self.event_bus.publish(
                event_job_id,
                {
                    **event,
                    "type": f"pilot.{event_type}",
                    "job_id": event_job_id,
                },
            )
        self.event_bus.publish(event_job_id, {"type": f"backend.{event_type}", "backend_event": event})

    def _publish_pilot_runtime_log(self, job_id: str, log: dict[str, Any]) -> None:
        if not isinstance(log, dict):
            log = {"message": str(log)}
        attempt_id = log.get("attempt_id") or self._active_pilot_attempt_by_job.get(job_id)
        if attempt_id:
            log = {**log, "attempt_id": attempt_id}
        self._publish_runtime_log(job_id, log)

    def _publish_pipeline_generated_if_available(
        self,
        job_id: str,
        *,
        pipeline: str | None = None,
        llm_metadata: dict[str, Any] | None = None,
    ) -> None:
        if job_id in self._published_pipeline_job_ids:
            return
        selected_pipeline = pipeline
        if selected_pipeline is None:
            job_manager: JobManager = _env_get(self.environment, "job_manager")
            job = job_manager.get_job(job_id)
            selected_pipeline = job.pipeline if job is not None else ""
        if not selected_pipeline:
            return
        selected_metadata = (
            llm_metadata
            or self._pipeline_llm_metadata_by_job.get(job_id)
            or {}
        )
        self.event_bus.publish(
            job_id,
            {
                "type": "pipeline.generated",
                "pipeline": selected_pipeline,
                "llm_metadata": selected_metadata,
            },
        )
        self._published_pipeline_job_ids.add(job_id)

    def _publish_runtime_log(self, job_id: str, log: dict[str, Any]) -> None:
        if not isinstance(log, dict):
            log = {"message": str(log)}
        key = (
            str(log.get("attempt_id", "")),
            str(log.get("timestamp", "")),
            str(log.get("step", "")),
            str(log.get("level", "")),
            str(log.get("message", "")),
        )
        published_keys = self._published_log_keys_by_job.setdefault(job_id, set())
        if key in published_keys:
            return
        published_keys.add(key)
        self.event_bus.publish(job_id, {"type": "log.appended", "log": log})

    def _persist_execution_log_context(self, job_id: str, execution: dict[str, Any]) -> None:
        log_context = {
            key: execution[key]
            for key in ("logs", "log_ref", "log_excerpt")
            if execution.get(key)
        }
        if not log_context:
            return
        job_manager: JobManager = _env_get(self.environment, "job_manager")
        job = job_manager.get_job(job_id)
        if job is None:
            return
        existing_result = job.result if isinstance(job.result, dict) else {}
        job_manager.update_result(job_id, {**existing_result, **log_context})


def _default_coordinator_factory(environment: Any, _broker: WebCheckpointBroker) -> RunCoordinator:
    return RunCoordinator(
        config=_env_get(environment, "config"),
        job_manager=_env_get(environment, "job_manager"),
        executor=_env_get(environment, "executor"),
        registry=_env_get(environment, "registry"),
        llm_provider=_env_get(environment, "llm_provider", None),
    )


def _default_pilot_controller_factory(environment: Any, _broker: WebCheckpointBroker) -> PilotController:
    return PilotController(
        config=_env_get(environment, "config"),
        job_manager=_env_get(environment, "job_manager"),
        executor=_env_get(environment, "executor"),
        registry=_env_get(environment, "registry"),
        asset_manager=_env_get(environment, "asset_manager"),
        llm_provider=_env_get(environment, "llm_provider", None),
    )


def _pilot_response_status(status: Any) -> str:
    normalized = str(status or "").lower()
    if normalized == "success":
        return "completed"
    if normalized in {"budget_exhausted", "failed"}:
        return "failed"
    return normalized or "failed"


def _build_pilot_summary_from_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {}
    best_attempt = max(
        attempts,
        key=lambda attempt: float(attempt.get("judge", {}).get("score", 0.0) or 0.0),
    )
    candidate_ids: list[str] = []
    for attempt in attempts:
        for candidate in attempt.get("candidates", []) or []:
            candidate_id = candidate.get("candidate_id") or candidate.get("id")
            if candidate_id and candidate_id not in candidate_ids:
                candidate_ids.append(candidate_id)
    return {
        "attempt_count": len(attempts),
        "first_attempt_id": attempts[0].get("attempt_id", ""),
        "best_attempt_id": best_attempt.get("attempt_id", ""),
        "final_attempt_id": attempts[-1].get("attempt_id", ""),
        "candidate_ids": candidate_ids,
    }


class _RealtimeLogExecutor:
    def __init__(
        self,
        executor: Any,
        log_handler: Callable[[dict[str, Any]], None],
    ) -> None:
        self.executor = executor
        self.log_handler = log_handler

    def execute(self, job_id: str, pipeline: str) -> dict[str, Any]:
        execute = self.executor.execute
        if "log_handler" in signature(execute).parameters:
            return execute(job_id, pipeline, log_handler=self.log_handler)
        return execute(job_id, pipeline)


class _EnvironmentProxy:
    def __init__(self, base: Any, executor: Any) -> None:
        self._base = base
        self.executor = executor

    def __getattr__(self, key: str) -> Any:
        return getattr(self._base, key)


def _environment_with_executor(environment: Any, executor: Any) -> Any:
    if isinstance(environment, dict):
        return {**environment, "executor": executor}
    return _EnvironmentProxy(environment, executor)


def _env_get(environment: Any, key: str, default: Any = None) -> Any:
    if isinstance(environment, dict):
        return environment.get(key, default)
    return getattr(environment, key, default)
