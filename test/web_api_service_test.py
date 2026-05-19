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


if __name__ == "__main__":
    unittest.main()
