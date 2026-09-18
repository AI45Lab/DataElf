from __future__ import annotations

import uuid
import json
import fcntl
from pathlib import Path
from collections import deque
from threading import Condition, RLock, Thread
from typing import Any, Callable, Protocol

from dataelf_server.scope_v2.client import ScopeV2AIIndexError as AIIndexError
from dataelf_server.workflows.pipeline import (
    ServerPipeline,
    PipelineError,
    recover_workspace_insights,
)
from dataelf_server.presentation.redaction import redact_message
from dataelf_server.presentation.schemas import JobRecord
from dataelf_server.settings import Settings
from dataelf_server.jobs.store import JobStore


class PipelineLike(Protocol):
    def run(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        workspace_path: Path,
        progress: Callable[[str, int], None],
        source_trace: Callable[[str | None], None],
    ) -> list[dict[str, Any]]:
        ...


class RetryJobError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class QueueFullError(RuntimeError):
    """Admission refused before creating a job or retry attempt."""


class JobManager:
    def __init__(
        self,
        settings: Settings,
        *,
        store: JobStore | None = None,
        pipeline: PipelineLike | None = None,
    ):
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.state_dir / "logs").mkdir(exist_ok=True)
        self._instance_lock = (settings.state_dir / "instance.lock").open("a+")
        try:
            fcntl.flock(self._instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._instance_lock.close()
            raise RuntimeError("Another DataElf Server owns this state directory") from None
        self.store = store or JobStore(settings.database_path)
        self.pipeline = pipeline or ServerPipeline(settings)
        self.store.fail_incomplete_jobs()
        self._closed = False
        self._lock = RLock()
        self._ready = Condition(self._lock)
        self._queue: deque[tuple[str, dict[str, Any], Path]] = deque()
        self._workers = [
            Thread(target=self._worker_loop, name=f"dataelf-job-{index + 1}", daemon=True)
            for index in range(settings.server.max_concurrent_jobs)
        ]
        self._remaining_workers = len(self._workers)
        for worker in self._workers:
            worker.start()

    def submit(self, request_payload: dict[str, Any]) -> JobRecord:
        with self._lock:
            if self._closed:
                raise RuntimeError("job manager is closed")
            self._check_queue_capacity()
            job_id = f"job_{uuid.uuid4().hex[:12]}"
            trace_id = uuid.uuid4().hex
            job_root = self.settings.workspaces_dir / job_id
            job_root.mkdir(parents=True)
            (job_root / "request.json").write_text(json.dumps(request_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            workspace_path = job_root / "attempts" / "0001"
            workspace_path.mkdir(parents=True)
            record = self.store.create_job(
                job_id=job_id,
                trace_id=trace_id,
                request_payload=request_payload,
                workspace_path=workspace_path,
            )
            self._queue.append((job_id, request_payload, workspace_path))
            self._ready.notify()
            return record

    def _check_queue_capacity(self) -> None:
        # Caller holds _lock through persistence and enqueue; workers dequeue
        # under the same lock, so concurrent admissions cannot overbook.
        if len(self._queue) >= self.settings.server.max_pending_jobs:
            raise QueueFullError("Pending job queue is full")

    def get(self, job_id: str) -> JobRecord | None:
        return self.store.get_job(job_id)

    def retry(self, job_id: str) -> JobRecord:
        with self._lock:
            if self._closed:
                raise RuntimeError("job manager is closed")
            previous = self.store.get_job(job_id)
            if previous is None:
                raise RetryJobError(
                    "job_not_found", "No job exists with the supplied job_id."
                )
            if previous.status != "failed":
                raise RetryJobError(
                    "job_not_retryable", "Only failed jobs can be retried."
                )

            self._check_queue_capacity()
            workspace_path = self.settings.workspaces_dir / job_id / "attempts" / f"{previous.attempt + 1:04d}"
            workspace_path.mkdir(parents=True, exist_ok=False)
            try:
                record = self.store.retry_job(
                    job_id=job_id, trace_id=uuid.uuid4().hex,
                    request_payload=previous.request, workspace_path=workspace_path,
                )
            except Exception:
                workspace_path.rmdir()  # Only the empty directory created above.
                raise

            self._queue.append((job_id, previous.request, workspace_path))
            self._ready.notify()
            return record

    def recover_result(self, job_id: str) -> JobRecord | None:
        """Recover complete artifacts from historical retry-parser false failures."""

        with self._lock:
            record = self.store.get_job(job_id)
            if (
                record is None
                or record.status != "failed"
                or record.error is None
                or record.error.reason
                not in {"invalid_agent_output", "model_terminated"}
            ):
                return record
            try:
                insights = recover_workspace_insights(
                    job_id, Path(record.workspace_path).resolve(), self.settings
                )
            except (OSError, PipelineError, ValueError, KeyError, TypeError):
                return record
            self.store.recover_job(job_id, insights)
            return self.store.get_job(job_id)

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.clear()
                self.store.fail_incomplete_jobs(code="service_shutdown", message="Service stopped before the job completed.")
                cancel = getattr(self.pipeline, "cancel", None)
                if callable(cancel):
                    cancel()
                self._ready.notify_all()
        if wait:
            for worker in self._workers:
                worker.join()

    def _worker_loop(self) -> None:
        try:
            while True:
                with self._ready:
                    self._ready.wait_for(lambda: self._closed or self._queue)
                    if self._closed:
                        return
                    # Dequeue and record the start atomically, preserving FIFO
                    # admission even when several slots become free together.
                    item = self._queue.popleft()
                    self.store.start_job(item[0])
                self._execute(*item)
        finally:
            with self._lock:
                self._remaining_workers -= 1
                if self._remaining_workers == 0:
                    self.store.close()
                    fcntl.flock(self._instance_lock, fcntl.LOCK_UN)
                    self._instance_lock.close()

    def _execute(
        self,
        job_id: str,
        request_payload: dict[str, Any],
        workspace_path: Path,
    ) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
            insights = self.pipeline.run(
                job_id=job_id,
                request_payload=request_payload,
                workspace_path=workspace_path,
                progress=lambda stage, value: self.store.update_progress(
                    job_id, stage=stage, progress=value
                ),
                source_trace=lambda value: self.store.set_source_trace_id(job_id, value),
            )
            if not self._closed:
                self.store.complete_job(job_id, insights)
        except PipelineError as exc:
            if not self._closed:
                self.store.fail_job(
                    job_id, code=exc.code, message=redact_message(exc.message)
                )
        except AIIndexError as exc:
            if not self._closed:
                if exc.trace_id:
                    self.store.set_source_trace_id(job_id, exc.trace_id)
                self.store.fail_job(
                    job_id,
                    code=f"ai_index_{exc.code}",
                    message=redact_message(exc.message),
                )
        except Exception as exc:
            if not self._closed:
                self.store.fail_job(
                    job_id,
                    code="internal_error",
                    message=redact_message(exc),
                )
