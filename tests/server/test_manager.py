from __future__ import annotations

import time
import sqlite3
from pathlib import Path
from threading import Event

import pytest

from dataelf_server.jobs import manager as manager_module
from dataelf_server.jobs.manager import JobManager, RetryJobError
from dataelf_server.workflows.pipeline import PipelineError
from tests.server.helpers import settings as Settings
from dataelf_server.jobs.store import JobStore
from tests.server.helpers import insight


class SuccessfulPipeline:
    def run(self, *, job_id, request_payload, workspace_path, progress, source_trace):
        progress("preparing_workspace", 5)
        progress("fetching_ai_index", 15)
        source_trace("source-trace")
        progress("source_ready", 30)
        progress("analyzing_with_pi", 40)
        progress("validating_insights", 90)
        return [insight(i) for i in range(1, 5)]


class FailingPipeline:
    def run(self, *, job_id, request_payload, workspace_path, progress, source_trace):
        progress("fetching_ai_index", 15)
        raise PipelineError("no_source_data", "No news records.")


class CancellablePipeline:
    def __init__(self):
        self.started = Event()
        self.released = Event()
        self.cancelled = False

    def run(self, *, job_id, request_payload, workspace_path, progress, source_trace):
        progress("analyzing_with_pi", 40)
        self.started.set()
        self.released.wait(timeout=5)
        return [insight(i) for i in range(1, 4)]

    def cancel(self):
        self.cancelled = True
        self.released.set()


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        state_dir=tmp_path / ".dataelf",
        ai_index_api_key="dummy-ai-key",
        openai_base_url="https://model.example/v1",
        openai_api_key="dummy-model-key",
    )


def wait_terminal(manager: JobManager, job_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = manager.get(job_id)
        if record and record.status in {"completed", "failed"}:
            return record
        time.sleep(0.01)
    raise AssertionError("job did not reach terminal state")


def test_manager_runs_job_and_persists_result(tmp_path: Path) -> None:
    manager = JobManager(make_settings(tmp_path), pipeline=SuccessfulPipeline())
    record = manager.submit({"query": "围绕 Wayve 发现 insight"})
    terminal = wait_terminal(manager, record.job_id)
    assert terminal.status == "completed"
    assert terminal.stage == "completed"
    assert terminal.progress == 100
    assert terminal.source_trace_id == "source-trace"
    assert len(terminal.insights or []) == 4
    assert terminal.started_at and terminal.started_at.endswith("Z")
    manager.close(wait=True)


def test_manager_records_pipeline_failure(tmp_path: Path) -> None:
    manager = JobManager(make_settings(tmp_path), pipeline=FailingPipeline())
    record = manager.submit({"query": "test"})
    terminal = wait_terminal(manager, record.job_id)
    assert terminal.status == "failed"
    assert terminal.progress == 15
    assert terminal.error and terminal.error.category == "source_error"
    assert terminal.error.reason == "no_data"
    manager.close(wait=True)


def test_store_marks_incomplete_jobs_failed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    store = JobStore(path)
    store.create_job(
        job_id="job_old",
        trace_id="trace-old",
        request_payload={"query": "test"},
        workspace_path=tmp_path / "workspace",
    )
    assert store.fail_incomplete_jobs() == 1
    record = store.get_job("job_old")
    assert record and record.status == "failed"
    assert record.error and record.error.category == "service_error"
    assert record.error.reason == "service_restarted"
    store.close()


def test_store_migrates_started_at_and_sets_it_only_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE jobs (
          job_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, status TEXT NOT NULL,
          stage TEXT NOT NULL, progress INTEGER NOT NULL, request_json TEXT NOT NULL,
          workspace_path TEXT NOT NULL, insights_json TEXT, source_trace_id TEXT,
          error_code TEXT, error_message TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = JobStore(path)
    created = store.create_job(
        job_id="job_migrated",
        trace_id="trace-migrated",
        request_payload={"query": "test"},
        workspace_path=tmp_path / "workspace",
    )
    assert created.started_at is None
    store.start_job("job_migrated")
    first = store.get_job("job_migrated")
    assert first and first.started_at and first.started_at.endswith("Z")
    store.update_progress("job_migrated", stage="analyzing_with_pi", progress=40)
    store.start_job("job_migrated")
    second = store.get_job("job_migrated")
    assert second and second.started_at == first.started_at
    store.close()


def test_manager_retry_archives_each_attempt_and_reuses_request(tmp_path: Path) -> None:
    class RecordingFailure:
        def __init__(self):
            self.requests = []

        def run(
            self, *, job_id, request_payload, workspace_path, progress, source_trace
        ):
            self.requests.append(request_payload)
            workspace_path.mkdir(parents=True, exist_ok=True)
            (workspace_path / "attempt.txt").write_text(
                str(len(self.requests)), encoding="utf-8"
            )
            progress("fetching_ai_index", 15)
            raise PipelineError("pi_error", "failed")

    pipeline = RecordingFailure()
    settings = make_settings(tmp_path)
    manager = JobManager(settings, pipeline=pipeline)
    payload = {"query": "生成综合总结", "scope": "scope_v2"}
    first = manager.submit(payload)
    wait_terminal(manager, first.job_id)

    second = manager.retry(first.job_id)
    assert second.job_id == first.job_id
    assert second.trace_id != first.trace_id
    assert second.request == payload
    assert second.started_at is None
    wait_terminal(manager, first.job_id)
    assert (settings.workspaces_dir / first.job_id / "attempts" / "0001" / "attempt.txt").read_text() == "1"

    manager.retry(first.job_id)
    wait_terminal(manager, first.job_id)
    assert (settings.workspaces_dir / first.job_id / "attempts" / "0002" / "attempt.txt").read_text() == "2"
    assert pipeline.requests == [payload, payload, payload]
    manager.close(wait=True)


def test_manager_retry_rejects_active_job_and_rolls_back_archive(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = JobStore(settings.database_path)
    manager = JobManager(settings, store=store, pipeline=SuccessfulPipeline())
    active = store.create_job(
        job_id="job_active",
        trace_id="trace-active",
        request_payload={"query": "test"},
        workspace_path=settings.workspaces_dir / "job_active",
    )
    with pytest.raises(RetryJobError) as caught:
        manager.retry(active.job_id)
    assert caught.value.code == "job_not_retryable"

    failed = store.create_job(
        job_id="job_rollback",
        trace_id="trace-rollback",
        request_payload={"query": "test"},
        workspace_path=settings.workspaces_dir / "job_rollback",
    )
    workspace = Path(failed.workspace_path)
    workspace.mkdir(parents=True)
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    store.fail_job("job_rollback", code="pi_error", message="failed")
    monkeypatch.setattr(
        store, "retry_job", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db"))
    )
    with pytest.raises(RuntimeError, match="db"):
        manager.retry("job_rollback")
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (settings.workspaces_dir / "job_rollback(1)").exists()
    manager.close(wait=True)


def test_manager_recovers_historical_retry_parser_failure(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = JobStore(settings.database_path)
    workspace = settings.workspaces_dir / "job_retry"
    store.create_job(
        job_id="job_retry",
        trace_id="trace-retry",
        request_payload={"query": "生成综合总结"},
        workspace_path=workspace,
    )
    store.fail_job(
        "job_retry",
        code="pi_event_parse_error",
        message="PI_MODEL_ERROR: terminated",
    )
    expected = [insight(i) for i in range(1, 4)]
    monkeypatch.setattr(
        manager_module,
        "recover_workspace_insights",
        lambda job_id, workspace_path, settings: expected,
    )
    manager = JobManager(settings, store=store, pipeline=SuccessfulPipeline())

    recovered = manager.recover_result("job_retry")

    assert recovered and recovered.status == "completed"
    assert recovered.progress == 100
    assert recovered.error is None
    assert recovered.insights == expected
    manager.close(wait=True)


def test_manager_immediate_close_cancels_active_job(tmp_path: Path) -> None:
    pipeline = CancellablePipeline()
    manager = JobManager(make_settings(tmp_path), pipeline=pipeline)
    submitted = manager.submit({"query": "long running task"})
    assert pipeline.started.wait(timeout=2)

    started = time.monotonic()
    manager.close(wait=False)
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert pipeline.cancelled is True
    manager.close(wait=True)
    reader = JobStore(manager.settings.database_path)
    record = reader.get_job(submitted.job_id)
    reader.close()
    assert record and record.status == "failed"
    assert record.error and record.error.category == "service_error"
    assert record.error.reason == "service_shutdown"
    pipeline.released.set()
    manager.close(wait=True)
