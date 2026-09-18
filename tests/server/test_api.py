from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi.routing import APIRoute

from dataelf_server.app import create_app
from dataelf_server.jobs.manager import RetryJobError
from dataelf_server.presentation.schemas import JobError, JobRecord
from tests.server.helpers import settings as Settings
from tests.server.helpers import insight


class StubManager:
    def __init__(self, settings):
        self.records = {}
        self.closed = False

    def submit(self, payload):
        record = make_record(request=payload)
        self.records[record.job_id] = record
        return record

    def get(self, job_id):
        return self.records.get(job_id)

    def retry(self, job_id):
        record = self.records.get(job_id)
        if record is None:
            raise RetryJobError("job_not_found", "missing")
        if record.status != "failed":
            raise RetryJobError("job_not_retryable", "not failed")
        retried = record.model_copy(
            update={
                "trace_id": "trace-retry",
                "status": "queued",
                "stage": "queued",
                "progress": 0,
                "insights": None,
                "error": None,
                "created_at": "2026-01-02T00:00:00Z",
                "started_at": None,
            }
        )
        self.records[job_id] = retried
        return retried

    def close(self, *, wait=False):
        self.closed = True


def make_record(**overrides) -> JobRecord:
    values = {
        "job_id": "job_test",
        "trace_id": "trace-test",
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "request": {"query": "test query"},
        "workspace_path": "/tmp/job_test",
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": None,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return JobRecord.model_validate(values)


def job_error(category: str, reason: str, message: str = "failed") -> JobError:
    return JobError(
        category=category,
        reason=reason,
        message=message,
        retryable=True,
        action="retry",
        stage="analyzing_with_pi",
    )


def configured_app(tmp_path: Path):
    settings = Settings(project_root=tmp_path, state_dir=tmp_path / ".dataelf")
    app = create_app(
        settings=settings,
        manager_factory=StubManager,
        validate_runtime=False,
    )
    app.state.manager = StubManager(settings)
    return app


def request(app, method: str, url: str, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(send())


def test_app_exposes_only_public_job_routes(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    routes = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert routes == [
        "/api/v1/insight/jobs",
        "/api/v1/insight/jobs/{job_id}/retry",
        "/api/v1/insight/jobs/{job_id}",
        "/api/v1/insight/jobs/{job_id}/result",
    ]


def test_submit_uses_natural_language_body(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    response = request(
        app,
        "POST",
        "/api/v1/insight/jobs",
        json={
            "query": "围绕 Wayve，发现最近值得关注的 3 个 insight",
            "scope": "scope_v2",
        },
    )
    stored = app.state.manager.records["job_test"]
    assert response.status_code == 202
    assert response.json()["data"] == {
        "job_id": "job_test",
        "status": "queued",
        "created_at": "2026-01-01T00:00:00Z",
    }
    assert stored.request["query"] == "围绕 Wayve，发现最近值得关注的 3 个 insight"
    assert stored.request["scope"] == "scope_v2"
    assert "insights_explorer" not in stored.request


def test_submit_defaults_to_scope_v2(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    response = request(
        app,
        "POST",
        "/api/v1/insight/jobs",
        json={"query": "生成开源社区模块总结"},
    )
    assert response.status_code == 202
    stored = app.state.manager.records["job_test"]
    assert stored.request == {
        "query": "生成开源社区模块总结",
        "scope": "scope_v2",
    }


def test_validation_errors_use_standard_envelope(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    response = request(app, "POST", "/api/v1/insight/jobs", json={"query": "   "})
    assert response.status_code == 422
    assert response.json()["code"] == 422
    assert response.json()["msg"] == "invalid_request"
    assert response.json()["data"]["error"]["category"] == "request_error"
    assert response.json()["trace_id"]


def test_status_unknown_and_not_ready_result(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    missing = request(app, "GET", "/api/v1/insight/jobs/missing")
    request(app, "POST", "/api/v1/insight/jobs", json={"query": "test"})
    pending = request(app, "GET", "/api/v1/insight/jobs/job_test/result")
    assert missing.status_code == 404
    assert missing.json()["code"] == 404
    assert missing.json()["msg"] == "job_not_found"
    assert pending.status_code == 409
    assert pending.json()["code"] == 409
    assert pending.json()["data"]["error"]["reason"] == "job_not_ready"


def test_status_returns_created_and_started_times(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    app.state.manager.records["job_running"] = make_record(
        job_id="job_running",
        status="running",
        stage="analyzing_with_pi",
        progress=40,
        started_at="2026-01-01T00:02:00Z",
    )

    response = request(app, "GET", "/api/v1/insight/jobs/job_running")

    assert response.status_code == 200
    assert response.json()["data"]["created_at"] == "2026-01-01T00:00:00Z"
    assert response.json()["data"]["started_at"] == "2026-01-01T00:02:00Z"


def test_retry_reuses_job_id_and_rejects_non_failed_jobs(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    app.state.manager.records["job_failed"] = make_record(
        job_id="job_failed",
        status="failed",
        stage="failed",
        error=job_error("analysis_error", "agent_analysis_failed"),
    )

    retried = request(app, "POST", "/api/v1/insight/jobs/job_failed/retry")
    active = request(app, "POST", "/api/v1/insight/jobs/job_failed/retry")
    missing = request(app, "POST", "/api/v1/insight/jobs/missing/retry")

    assert retried.status_code == 202
    assert retried.json()["trace_id"] == "trace-retry"
    assert retried.json()["data"] == {
        "job_id": "job_failed",
        "status": "queued",
        "created_at": "2026-01-02T00:00:00Z",
    }
    assert active.status_code == 409
    assert active.json()["code"] == 409
    assert active.json()["msg"] == "job_not_retryable"
    assert missing.status_code == 404
    assert missing.json()["code"] == 404


def test_completed_and_failed_results(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    app.state.manager.records["job_done"] = make_record(
        job_id="job_done",
        status="completed",
        stage="completed",
        progress=100,
        insights=[insight(i) for i in range(1, 5)],
        started_at="2026-01-01T00:02:00Z",
    )
    completed = request(app, "GET", "/api/v1/insight/jobs/job_done/result")
    app.state.manager.records["job_failed"] = make_record(
        job_id="job_failed",
        status="failed",
        stage="failed",
        progress=15,
        error=job_error("source_error", "no_data", "AI Index 未返回数据。"),
    )
    failed = request(app, "GET", "/api/v1/insight/jobs/job_failed/result")
    assert completed.status_code == 200
    completed_data = completed.json()["data"]
    assert set(completed_data) == {"job_id", "created_at", "started_at", "insights"}
    assert completed_data["job_id"] == "job_done"
    assert completed_data["started_at"] == "2026-01-01T00:02:00Z"
    assert len(completed_data["insights"]) == 4
    assert set(completed_data["insights"][0]) == {
        "insight_id",
        "title",
        "content",
        "sources",
    }
    assert completed_data["insights"][0]["title"] == "Insight 1"
    assert completed_data["insights"][0]["content"] == "Grounded thesis 1"
    assert completed_data["insights"][0]["sources"] == []
    assert failed.status_code == 422
    assert failed.json()["code"] == 422
    assert failed.json()["data"]["error"]["category"] == "source_error"
    assert failed.json()["data"]["error"]["reason"] == "no_data"
