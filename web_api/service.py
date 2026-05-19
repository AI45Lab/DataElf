from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from agentic.controller import RunCoordinator
from runtime import JobManager, JobStatus

from .checkpoints import WebCheckpointBroker
from .command_parser import parse_user_command
from .events import JobEventBus
from .run_preflight import (
    append_dataset_to_task,
    build_dataset_selection_payload,
    needs_dataset_selection,
    resolve_dataset_answer,
)


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


class RunWebService:
    def __init__(
        self,
        *,
        environment: Any,
        event_bus: JobEventBus | None = None,
        checkpoint_broker: WebCheckpointBroker | None = None,
        coordinator_factory: CoordinatorFactory | None = None,
        run_in_background: bool = True,
    ) -> None:
        self.environment = environment
        self.event_bus = event_bus or JobEventBus()
        self.checkpoint_broker = checkpoint_broker or WebCheckpointBroker()
        self.coordinator_factory = coordinator_factory or _default_coordinator_factory
        self.run_in_background = run_in_background

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
        return None if job is None else job.to_dict()

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
        return [
            {"name": name, "columns": columns}
            for name, columns in sorted(dataset_schemas.items())
        ]

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
            coordinator = self.coordinator_factory(self.environment, self.checkpoint_broker)
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
                self.event_bus.publish(
                    job_id,
                    {
                        "type": "pipeline.generated",
                        "pipeline": pipeline,
                        "llm_metadata": result.get("llm_metadata", {}),
                    },
                )
            execution = result.get("execution", {})
            for log in execution.get("logs", []) if isinstance(execution, dict) else []:
                self.event_bus.publish(job_id, {"type": "log.appended", "log": log})
            if result.get("status") == "completed":
                job_manager.update_status(job_id, JobStatus.COMPLETED)
                final_type = "job.completed"
            else:
                error = execution.get("error") if isinstance(execution, dict) else None
                job_manager.update_error(job_id, error or "Run failed.")
                final_type = "job.failed"
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
            return {"job_id": job_id, "status": "failed", "execution": {"error": error}}

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
        self.event_bus.publish(job_id, {"type": f"backend.{event_type}", "backend_event": event})


def _default_coordinator_factory(environment: Any, _broker: WebCheckpointBroker) -> RunCoordinator:
    return RunCoordinator(
        config=_env_get(environment, "config"),
        job_manager=_env_get(environment, "job_manager"),
        executor=_env_get(environment, "executor"),
        registry=_env_get(environment, "registry"),
        llm_provider=_env_get(environment, "llm_provider", None),
    )


def _env_get(environment: Any, key: str, default: Any = None) -> Any:
    if isinstance(environment, dict):
        return environment.get(key, default)
    return getattr(environment, key, default)
