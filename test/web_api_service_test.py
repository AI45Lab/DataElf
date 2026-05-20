from dataclasses import dataclass
import tempfile
from pathlib import Path
import unittest

from runtime import JobManager
from web_api.events import JobEventBus
from web_api.service import RunSubmission, RunWebService
from web_api.checkpoints import WebCheckpointBroker


@dataclass
class FakeEnvironment:
    job_manager: JobManager
    dataset_schemas: dict
    registry: object


class FakeRegistry:
    def list_schemas(self):
        return [{"name": "security_audit", "description": "Audit data", "parameters": {}}]


class FakeCoordinator:
    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "job_id": kwargs["job_id"],
            "status": "completed",
            "pipeline": 'save_result({"ok": True})',
            "execution": {"success": True, "result": {"ok": True}, "error": None},
            "clarification": {"status": "not_requested"},
            "capability_gap": {},
        }


class PipelineBeforeExecutionCoordinator:
    def __init__(self, job_manager):
        self.job_manager = job_manager

    def execute(self, **kwargs):
        job_id = kwargs["job_id"]
        event_handler = kwargs["event_handler"]
        pipeline = 'log_step("hello")\nsave_result({"ok": True})'
        self.job_manager.update_pipeline(job_id, pipeline)
        event_handler({
            "type": "stage_completed",
            "stage": "pipeline_generation",
            "llm": {"model": "fake-model", "elapsed_seconds": 0.1},
        })
        event_handler({
            "type": "stage_started",
            "stage": "execution",
        })
        event_handler({
            "type": "stage_completed",
            "stage": "execution",
            "success": True,
        })
        return {
            "job_id": job_id,
            "status": "completed",
            "pipeline": pipeline,
            "execution": {"success": True, "result": {"ok": True}, "error": None},
            "clarification": {"status": "not_requested"},
            "capability_gap": {},
        }


class AutoAnswerCheckpointBroker(WebCheckpointBroker):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def create_checkpoint(self, **kwargs):
        checkpoint = super().create_checkpoint(**kwargs)
        self.answer_checkpoint(
            job_id=checkpoint.job_id,
            checkpoint_id=checkpoint.checkpoint_id,
            answer=self.answer,
        )
        return checkpoint


class InspectingCheckpointBroker(WebCheckpointBroker):
    def __init__(self, answer, before_answer):
        super().__init__()
        self.answer = answer
        self.before_answer = before_answer

    def wait_for_answer(self, **kwargs):
        self.before_answer()
        return self.answer


class WebApiServiceTest(unittest.TestCase):
    def test_run_service_creates_job_and_publishes_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            job_manager = JobManager(jobs_dir=Path(tmp_dir) / ".jobs")
            event_bus = JobEventBus()
            coordinator = FakeCoordinator()
            service = RunWebService(
                environment=FakeEnvironment(
                    job_manager=job_manager,
                    dataset_schemas={"security_audit_samples": ["text"]},
                    registry=FakeRegistry(),
                ),
                event_bus=event_bus,
                coordinator_factory=lambda _environment, _broker: coordinator,
                run_in_background=False,
            )

            submitted = service.submit_run(
                RunSubmission(command='elf run "run security_audit on security_audit_samples"')
            )

            self.assertEqual(submitted.mode, "run")
            self.assertEqual(submitted.task, "run security_audit on security_audit_samples")
            self.assertEqual(submitted.status, "completed")
            self.assertEqual(submitted.stream_url, f"/api/v1/jobs/{submitted.job_id}/events")
            self.assertEqual(job_manager.get_job(submitted.job_id).status.value, "completed")
            self.assertEqual(
                coordinator.calls[0]["task"],
                "run security_audit on security_audit_samples",
            )
            self.assertEqual(
                [event["type"] for event in event_bus.replay(submitted.job_id)],
                ["job.created", "job.running", "pipeline.generated", "job.completed"],
            )

    def test_run_service_rejects_non_run_modes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = RunWebService(
                environment=FakeEnvironment(
                    job_manager=JobManager(jobs_dir=Path(tmp_dir) / ".jobs"),
                    dataset_schemas={},
                    registry=FakeRegistry(),
                ),
                event_bus=JobEventBus(),
                coordinator_factory=lambda _environment, _broker: FakeCoordinator(),
                run_in_background=False,
            )

            response = service.submit_run(RunSubmission(command="elf pilot improve this"))

            self.assertEqual(response.mode, "pilot")
            self.assertEqual(response.status, "unsupported")
            self.assertEqual(response.task, "improve this")

    def test_run_service_requests_dataset_when_task_has_no_dataset(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            job_manager = JobManager(jobs_dir=Path(tmp_dir) / ".jobs")
            event_bus = JobEventBus()
            coordinator = FakeCoordinator()
            service = RunWebService(
                environment=FakeEnvironment(
                    job_manager=job_manager,
                    dataset_schemas={"security_audit_samples": ["text"]},
                    registry=FakeRegistry(),
                ),
                event_bus=event_bus,
                checkpoint_broker=AutoAnswerCheckpointBroker(
                    {"decision": "answer", "answer": "security_audit_samples"}
                ),
                coordinator_factory=lambda _environment, _broker: coordinator,
                run_in_background=False,
            )

            submitted = service.submit_run(RunSubmission(command="run security_audit"))

            self.assertEqual(submitted.status, "completed")
            self.assertIn(
                "Use dataset security_audit_samples as dataset_name.",
                coordinator.calls[0]["task"],
            )
            events = event_bus.replay(submitted.job_id)
            checkpoint_events = [
                event for event in events if event["type"] == "checkpoint.created"
            ]
            self.assertEqual(len(checkpoint_events), 1)
            self.assertEqual(
                checkpoint_events[0]["checkpoint_type"],
                "dataset_selection",
            )
            self.assertEqual(
                checkpoint_events[0]["payload"]["options"],
                ["security_audit_samples"],
            )

    def test_dataset_checkpoint_is_persisted_as_paused_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            job_manager = JobManager(jobs_dir=Path(tmp_dir) / ".jobs")
            observed_states = []

            def observe_waiting_state():
                jobs = job_manager.list_jobs()
                self.assertEqual(len(jobs), 1)
                observed_states.append(jobs[0].to_dict())

            service = RunWebService(
                environment=FakeEnvironment(
                    job_manager=job_manager,
                    dataset_schemas={"security_audit_samples": ["text"]},
                    registry=FakeRegistry(),
                ),
                event_bus=JobEventBus(),
                checkpoint_broker=InspectingCheckpointBroker(
                    {"decision": "answer", "answer": "security_audit_samples"},
                    observe_waiting_state,
                ),
                coordinator_factory=lambda _environment, _broker: FakeCoordinator(),
                run_in_background=False,
            )

            service.submit_run(RunSubmission(command="run"))

            self.assertEqual(observed_states[0]["status"], "paused")
            self.assertEqual(observed_states[0]["checkpoint_type"], "dataset_selection")
            self.assertEqual(observed_states[0]["checkpoint_state"], "pending")
            self.assertEqual(
                observed_states[0]["checkpoint_payload"]["options"],
                ["security_audit_samples"],
            )
            self.assertTrue(
                observed_states[0]["checkpoint_payload"]["checkpoint_id"].startswith("chk_")
            )

    def test_pipeline_event_is_published_before_execution_stage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            job_manager = JobManager(jobs_dir=Path(tmp_dir) / ".jobs")
            event_bus = JobEventBus()
            coordinator = PipelineBeforeExecutionCoordinator(job_manager)
            service = RunWebService(
                environment=FakeEnvironment(
                    job_manager=job_manager,
                    dataset_schemas={"security_audit_samples": ["text"]},
                    registry=FakeRegistry(),
                ),
                event_bus=event_bus,
                coordinator_factory=lambda _environment, _broker: coordinator,
                run_in_background=False,
            )

            submitted = service.submit_run(
                RunSubmission(command="run security_audit on security_audit_samples")
            )

            events = event_bus.replay(submitted.job_id)
            pipeline_index = next(
                index for index, event in enumerate(events)
                if event["type"] == "pipeline.generated"
            )
            execution_index = next(
                index for index, event in enumerate(events)
                if (
                    event["type"] == "backend.stage_started"
                    and event["backend_event"]["stage"] == "execution"
                )
            )
            self.assertLess(pipeline_index, execution_index)
            self.assertEqual(
                events[pipeline_index]["pipeline"],
                'log_step("hello")\nsave_result({"ok": True})',
            )
            self.assertEqual(
                events[pipeline_index]["llm_metadata"]["model"],
                "fake-model",
            )

    def test_execution_stage_events_are_published_as_log_lines(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            job_manager = JobManager(jobs_dir=Path(tmp_dir) / ".jobs")
            event_bus = JobEventBus()
            coordinator = PipelineBeforeExecutionCoordinator(job_manager)
            service = RunWebService(
                environment=FakeEnvironment(
                    job_manager=job_manager,
                    dataset_schemas={"security_audit_samples": ["text"]},
                    registry=FakeRegistry(),
                ),
                event_bus=event_bus,
                coordinator_factory=lambda _environment, _broker: coordinator,
                run_in_background=False,
            )

            submitted = service.submit_run(
                RunSubmission(command="run security_audit on security_audit_samples")
            )

            log_messages = [
                event["log"]
                for event in event_bus.replay(submitted.job_id)
                if event["type"] == "log.appended"
            ]
            self.assertIn(
                {
                    "source": "stage",
                    "level": "INFO",
                    "message": "Execution: Running pipeline",
                },
                log_messages,
            )
            self.assertIn(
                {
                    "source": "stage",
                    "level": "SUCCESS",
                    "message": "Execution: status=success",
                    "icon": "✅",
                },
                log_messages,
            )


if __name__ == "__main__":
    unittest.main()
