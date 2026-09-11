from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from dataelf_server.presentation.errors import classify_job_error, infer_failure_stage
from dataelf_server.presentation.schemas import JobError, JobRecord


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  job_id TEXT PRIMARY KEY,
                  attempt INTEGER NOT NULL DEFAULT 1,
                  trace_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  stage TEXT NOT NULL,
                  progress INTEGER NOT NULL,
                  request_json TEXT NOT NULL,
                  workspace_path TEXT NOT NULL,
                  insights_json TEXT,
                  source_trace_id TEXT,
                  error_code TEXT,
                  error_message TEXT,
                  error_category TEXT,
                  error_reason TEXT,
                  error_retryable INTEGER,
                  error_action TEXT,
                  error_stage TEXT,
                  error_details_json TEXT,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "attempt" not in columns:
                self._connection.execute("ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
            if "started_at" not in columns:
                self._connection.execute("ALTER TABLE jobs ADD COLUMN started_at TEXT")
            error_columns = {
                "error_category": "TEXT",
                "error_reason": "TEXT",
                "error_retryable": "INTEGER",
                "error_action": "TEXT",
                "error_stage": "TEXT",
                "error_details_json": "TEXT",
            }
            for name, sql_type in error_columns.items():
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}"
                    )
            self._connection.execute(
                """
                UPDATE jobs
                SET created_at=REPLACE(created_at, '+00:00', 'Z'),
                    started_at=REPLACE(started_at, '+00:00', 'Z'),
                    updated_at=REPLACE(updated_at, '+00:00', 'Z')
                WHERE created_at LIKE '%+00:00'
                   OR started_at LIKE '%+00:00'
                   OR updated_at LIKE '%+00:00'
                """
            )
            self._connection.execute("CREATE TABLE IF NOT EXISTS attempts AS SELECT * FROM jobs WHERE 0")
            self._connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS attempt_identity ON attempts(job_id, attempt)")
            # Snapshot every transition in the same transaction as the public job row.
            for operation in ("INSERT", "UPDATE"):
                self._connection.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS snapshot_attempt_{operation.lower()}
                    AFTER {operation} ON jobs BEGIN
                      INSERT OR REPLACE INTO attempts SELECT * FROM jobs WHERE job_id=NEW.job_id;
                    END
                """)
            self._connection.commit()

    def list_attempts(self, job_id: str) -> list[JobRecord]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY attempt", (job_id,)).fetchall()
        return [self._row_to_job(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_job(
        self,
        *,
        job_id: str,
        trace_id: str,
        request_payload: dict[str, Any],
        workspace_path: Path,
    ) -> JobRecord:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO jobs(
                  job_id, trace_id, status, stage, progress, request_json,
                  workspace_path, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    trace_id,
                    json.dumps(request_payload, ensure_ascii=False),
                    str(workspace_path),
                    now,
                    now,
                ),
            )
            self._connection.commit()
        record = self.get_job(job_id)
        assert record is not None
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def update_progress(self, job_id: str, *, stage: str, progress: int) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE jobs
                SET status='running', stage=?, progress=?, updated_at=?
                WHERE job_id=? AND status NOT IN ('completed', 'failed')
                """,
                (stage, progress, utc_now(), job_id),
            )
            self._connection.commit()

    def start_job(self, job_id: str) -> None:
        """Mark the instant a queued job is first taken by the worker."""

        now = utc_now()
        with self._lock:
            self._connection.execute(
                """
                UPDATE jobs
                SET status='running', stage='preparing_workspace', progress=5,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE job_id=? AND status='queued'
                """,
                (now, now, job_id),
            )
            self._connection.commit()

    def retry_job(
        self,
        *,
        job_id: str,
        trace_id: str,
        request_payload: dict[str, Any],
        workspace_path: Path,
    ) -> JobRecord:
        """Reset a failed job as a new queued attempt under the same public ID."""

        now = utc_now()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET attempt=attempt+1, trace_id=?, status='queued', stage='queued', progress=0,
                    request_json=?, workspace_path=?, insights_json=NULL,
                    source_trace_id=NULL, error_code=NULL, error_message=NULL,
                    error_category=NULL, error_reason=NULL,
                    error_retryable=NULL, error_action=NULL, error_stage=NULL,
                    error_details_json=NULL,
                    created_at=?, started_at=NULL, updated_at=?
                WHERE job_id=? AND status='failed'
                """,
                (
                    trace_id,
                    json.dumps(request_payload, ensure_ascii=False),
                    str(workspace_path),
                    now,
                    now,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                raise ValueError("job is not retryable")
            self._connection.commit()
        record = self.get_job(job_id)
        assert record is not None
        return record

    def set_source_trace_id(self, job_id: str, source_trace_id: str | None) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE jobs SET source_trace_id=?, updated_at=? WHERE job_id=? AND status IN ('queued', 'running')",
                (source_trace_id, utc_now(), job_id),
            )
            self._connection.commit()

    def complete_job(self, job_id: str, insights: list[dict[str, Any]]) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE jobs
                SET status='completed', stage='completed', progress=100,
                    insights_json=?, error_code=NULL, error_message=NULL,
                    error_category=NULL, error_reason=NULL,
                    error_retryable=NULL, error_action=NULL, error_stage=NULL,
                    error_details_json=NULL, updated_at=?
                WHERE job_id=? AND status NOT IN ('completed', 'failed')
                """,
                (json.dumps(insights, ensure_ascii=False), utc_now(), job_id),
            )
            self._connection.commit()

    def fail_job(self, job_id: str, *, code: str, message: str) -> None:
        with self._lock:
            current = self._connection.execute(
                "SELECT stage, progress FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            stage = infer_failure_stage(
                int(current["progress"]) if current else 0,
                str(current["stage"]) if current else None,
            )
            error = classify_job_error(code, message, stage)
            self._connection.execute(
                """
                UPDATE jobs SET status='failed', stage='failed',
                    error_code=?, error_message=?, error_category=?,
                    error_reason=?, error_retryable=?, error_action=?,
                    error_stage=?, error_details_json=?, updated_at=?
                WHERE job_id=? AND status NOT IN ('completed', 'failed')
                """,
                (
                    code,
                    error.message,
                    error.category,
                    error.reason,
                    int(error.retryable),
                    error.action,
                    error.stage,
                    json.dumps(error.details, ensure_ascii=False)
                    if error.details is not None
                    else None,
                    utc_now(),
                    job_id,
                ),
            )
            self._connection.commit()

    def recover_job(self, job_id: str, insights: list[dict[str, Any]]) -> None:
        """Promote a failed job whose complete workspace artifacts validate."""

        with self._lock:
            self._connection.execute(
                """
                UPDATE jobs
                SET status='completed', stage='completed', progress=100,
                    insights_json=?, error_code=NULL, error_message=NULL,
                    error_category=NULL, error_reason=NULL,
                    error_retryable=NULL, error_action=NULL, error_stage=NULL,
                    error_details_json=NULL, updated_at=?
                WHERE job_id=? AND status='failed'
                """,
                (json.dumps(insights, ensure_ascii=False), utc_now(), job_id),
            )
            self._connection.commit()

    def fail_incomplete_jobs(
        self,
        *,
        code: str = "service_restarted",
        message: str = "Service restarted before the job completed.",
    ) -> int:
        with self._lock:
            error = classify_job_error(code, message, "service")
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET status='failed', stage='failed', error_code=?,
                    error_message=?, error_category=?, error_reason=?,
                    error_retryable=?, error_action=?, error_stage=?,
                    error_details_json=?, updated_at=?
                WHERE status IN ('queued', 'running')
                """,
                (
                    code,
                    error.message,
                    error.category,
                    error.reason,
                    int(error.retryable),
                    error.action,
                    error.stage,
                    None,
                    utc_now(),
                ),
            )
            self._connection.commit()
            return cursor.rowcount

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        error = None
        if row["error_code"]:
            if row["error_category"] and row["error_reason"]:
                details = (
                    json.loads(row["error_details_json"])
                    if row["error_details_json"]
                    else None
                )
                error = JobError(
                    category=row["error_category"],
                    reason=row["error_reason"],
                    message=row["error_message"] or "",
                    retryable=bool(row["error_retryable"]),
                    action=row["error_action"] or "retry",
                    stage=row["error_stage"]
                    or infer_failure_stage(row["progress"], row["stage"]),
                    details=details,
                )
            else:
                legacy = classify_job_error(
                    row["error_code"],
                    row["error_message"] or "",
                    infer_failure_stage(row["progress"], row["stage"]),
                )
                error = JobError.model_validate(legacy.to_dict())
        return JobRecord(
            job_id=row["job_id"],
            attempt=row["attempt"],
            trace_id=row["trace_id"],
            status=row["status"],
            stage=row["stage"],
            progress=row["progress"],
            request=json.loads(row["request_json"]),
            workspace_path=row["workspace_path"],
            insights=json.loads(row["insights_json"]) if row["insights_json"] else None,
            source_trace_id=row["source_trace_id"],
            error=error,
            created_at=row["created_at"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
        )
