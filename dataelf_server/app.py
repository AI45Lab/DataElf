from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from dataelf_server.presentation.errors import request_error, service_error
from dataelf_server.jobs.manager import JobManager, RetryJobError, QueueFullError
from dataelf_server.presentation.insights import public_insights
from dataelf_server.presentation.schemas import InsightQueryRequest, envelope
from dataelf_server.settings import Settings
from dataelf_server.runtime.logging import service_logging


ManagerFactory = Callable[[Settings], JobManager]


def create_app(
    *,
    settings: Settings | None = None,
    manager_factory: ManagerFactory = JobManager,
    validate_runtime: bool = True,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if validate_runtime:
            resolved_settings.validate_runtime()
        manager = manager_factory(resolved_settings)
        app.state.manager = manager
        with service_logging(resolved_settings):
            try:
                yield
            finally:
                manager.close(wait=True)

    app = FastAPI(
        title="DataElf Server",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = uuid.uuid4().hex
        return _error_response(
            status_code=422,
            reason="invalid_request",
            message="请求内容不符合接口要求，请检查后重新提交。",
            trace_id=trace_id,
            details=[
                {key: value for key, value in error.items() if key not in {"ctx", "input"}}
                for error in exc.errors()
            ],
        )

    @app.exception_handler(QueueFullError)
    async def queue_full_handler(request: Request, exc: QueueFullError) -> JSONResponse:
        response = _error_response(
            status_code=503, reason="queue_full",
            message="任务队列已满，请稍后重新提交。", trace_id=uuid.uuid4().hex,
            category="service_error", retryable=True, action="retry_later",
        )
        response.headers["Retry-After"] = "30"
        return response

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = uuid.uuid4().hex
        return _error_response(
            status_code=500,
            reason="internal_error",
            message="服务处理请求时发生内部错误，请稍后重试。",
            trace_id=trace_id,
            category="service_error",
            retryable=True,
            action="retry",
        )

    @app.post("/api/v1/insight/jobs", status_code=202)
    async def submit_job(payload: InsightQueryRequest, request: Request) -> dict[str, Any]:
        manager: JobManager = request.app.state.manager
        record = manager.submit(payload.model_dump(exclude_none=True))
        return envelope(
            code=0,
            msg="accepted",
            trace_id=record.trace_id,
            data={
                "job_id": record.job_id,
                "status": "queued",
                "created_at": record.created_at,
            },
        )

    @app.post("/api/v1/insight/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str, request: Request) -> Any:
        manager: JobManager = request.app.state.manager
        try:
            record = manager.retry(job_id)
        except RetryJobError as exc:
            return _error_response(
                status_code=404 if exc.code == "job_not_found" else 409,
                reason=exc.code,
                message=exc.message,
                trace_id=uuid.uuid4().hex,
                retryable=False,
            )
        return envelope(
            code=0,
            msg="accepted",
            trace_id=record.trace_id,
            data={
                "job_id": record.job_id,
                "status": "queued",
                "created_at": record.created_at,
            },
        )

    @app.get("/api/v1/insight/jobs/{job_id}")
    async def get_job_status(job_id: str, request: Request) -> Any:
        manager: JobManager = request.app.state.manager
        record = manager.get(job_id)
        if record is None:
            return _error_response(
                status_code=404,
                reason="job_not_found",
                message="未找到指定的任务，请检查 job_id。",
                trace_id=uuid.uuid4().hex,
            )
        return envelope(
            code=0,
            msg="success",
            trace_id=record.trace_id,
            data={
                "job_id": record.job_id,
                "status": record.status,
                "stage": record.stage,
                "progress": record.progress,
                "created_at": record.created_at,
                "started_at": record.started_at,
                "error": record.error.model_dump(mode="json") if record.error else None,
            },
        )

    @app.get("/api/v1/insight/jobs/{job_id}/result")
    async def get_job_result(job_id: str, request: Request) -> Any:
        manager: JobManager = request.app.state.manager
        record = manager.get(job_id)
        if record is None:
            return _error_response(
                status_code=404,
                reason="job_not_found",
                message="未找到指定的任务，请检查 job_id。",
                trace_id=uuid.uuid4().hex,
            )
        if record.status in {"queued", "running"}:
            return _error_response(
                status_code=409,
                reason="job_not_ready",
                message="任务尚未完成，请继续查询任务状态。",
                trace_id=record.trace_id,
                retryable=True,
                action="poll_status",
                stage=record.stage,
                details={"status": record.status, "stage": record.stage, "progress": record.progress},
            )
        if record.status == "failed":
            recover = getattr(manager, "recover_result", None)
            if callable(recover):
                record = recover(job_id) or record
        if record.status == "failed" or record.insights is None:
            return JSONResponse(
                status_code=422,
                content=envelope(
                    code=422,
                    msg="job_failed",
                    trace_id=record.trace_id,
                    data={
                        "job_id": record.job_id,
                        "error": record.error.model_dump(mode="json")
                        if record.error
                        else service_error(
                            message="任务未生成可用的 Insight 结果，请稍后重试。"
                        ),
                    },
                ),
            )
        return envelope(
            code=0,
            msg="success",
            trace_id=record.trace_id,
            data={
                "job_id": record.job_id,
                "created_at": record.created_at,
                "started_at": record.started_at,
                "insights": public_insights(
                    record.insights, Path(record.workspace_path)
                )
            },
        )

    return app


def _error_response(
    *,
    status_code: int,
    reason: str,
    message: str,
    trace_id: str,
    category: str = "request_error",
    retryable: bool = False,
    action: str = "revise_request",
    stage: str = "request",
    details: Any = None,
) -> JSONResponse:
    if category == "service_error":
        error = service_error(message=message)
        error.update(reason=reason, stage=stage, retryable=retryable, action=action, details=details)
    else:
        error = request_error(
            reason=reason,
            message=message,
            stage=stage,
            retryable=retryable,
            action=action,
            details=details,
        )
    return JSONResponse(
        status_code=status_code,
        content=envelope(
            code=status_code,
            msg=reason,
            trace_id=trace_id,
            data={"error": error},
        ),
    )


