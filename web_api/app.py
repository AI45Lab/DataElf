from __future__ import annotations

from typing import Any

from cli.common import bootstrap_environment

from .service import RunSubmission, RunWebService

try:
    from fastapi import Request
except ImportError:  # pragma: no cover - create_app raises a clearer dependency error.
    Request = Any  # type: ignore


def create_app(
    *,
    config_path: str | None = None,
    prefix: str | None = None,
    service: RunWebService | None = None,
):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import StreamingResponse
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI web dependencies are not installed. "
            "Install them with: uv pip install fastapi uvicorn httpx"
        ) from exc

    if service is None:
        environment = bootstrap_environment(config_path=config_path, prefix=prefix)
        service = RunWebService(environment=environment)

    app = FastAPI(title="DataElf Web API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.run_service = service

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/sessions")
    def list_sessions() -> dict[str, Any]:
        return {"sessions": service.list_sessions()}

    @app.post("/api/v1/sessions")
    def create_session(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return service.create_session(payload or {})

    @app.get("/api/v1/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        session = service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.patch("/api/v1/sessions/{session_id}")
    def update_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = service.update_session(session_id, payload)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.delete("/api/v1/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, bool]:
        deleted = service.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="session not found")
        return {"deleted": True}

    @app.post("/api/v1/sessions/{session_id}/mode")
    def set_session_mode(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            session = service.set_session_mode(session_id, str(payload.get("mode", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.post("/api/v1/sessions/{session_id}/snapshot")
    def save_session_snapshot(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = payload.get("snapshot", payload)
        if not isinstance(snapshot, dict):
            raise HTTPException(status_code=400, detail="snapshot must be an object")
        session = service.save_session_snapshot(session_id, snapshot)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    @app.post("/api/v1/sessions/{session_id}/runs")
    def submit_session_run(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command", "")).strip()
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        budget_steps = payload.get("budget_steps")
        if budget_steps is not None:
            try:
                budget_steps = int(budget_steps)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="budget_steps must be an integer") from exc
            if budget_steps < 1 or budget_steps > 10:
                raise HTTPException(status_code=400, detail="budget_steps must be between 1 and 10")
        try:
            response = service.submit_session_run(
                session_id,
                command=command,
                budget_steps=budget_steps,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return response.__dict__

    @app.post("/api/v1/runs")
    def submit_run(payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command", "")).strip()
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        response = service.submit_run(
            RunSubmission(
                command=command,
                session_id=payload.get("session_id"),
            )
        )
        if response.status == "unsupported":
            raise HTTPException(
                status_code=400,
                detail=f"Mode '{response.mode}' is not supported by the Run API.",
            )
        return response.__dict__

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/api/v1/jobs/{job_id}/events")
    def stream_job_events(job_id: str, request: Request):
        if service.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        last_event_id = _parse_last_event_id(request)
        return StreamingResponse(
            service.event_bus.sse_lines(job_id, after_event_id=last_event_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/v1/jobs/{job_id}/events/replay")
    def replay_job_events(job_id: str, request: Request) -> dict[str, Any]:
        if service.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        last_event_id = _parse_last_event_id(request)
        return {"events": service.event_bus.replay(job_id, after_event_id=last_event_id)}

    @app.post("/api/v1/jobs/{job_id}/checkpoints/{checkpoint_id}/answer")
    def answer_checkpoint(
        job_id: str,
        checkpoint_id: str,
        payload: dict[str, Any],
    ) -> dict[str, bool]:
        answer = {
            "decision": payload.get("decision", "answer"),
            "answer": payload.get("answer", ""),
            "approved": bool(payload.get("approved", False)),
        }
        accepted = service.answer_checkpoint(
            job_id=job_id,
            checkpoint_id=checkpoint_id,
            answer=answer,
        )
        if not accepted:
            raise HTTPException(status_code=404, detail="checkpoint not found")
        return {"accepted": True}

    @app.get("/api/v1/datasets")
    def list_datasets() -> dict[str, Any]:
        return {"datasets": service.list_datasets()}

    @app.get("/api/v1/tools")
    def list_tools() -> dict[str, Any]:
        return {"tools": service.list_tools()}

    return app


def _parse_last_event_id(request: Any) -> int | None:
    value = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
