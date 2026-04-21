import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from config import Config


class JobStatus(str, Enum):

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    job_id: str
    task: str
    mode: str = "run"
    status: JobStatus = JobStatus.PENDING
    pipeline: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    parent_asset_id: str = ""
    attempt_count: int = 0
    final_score: float = 0.0
    capability_gap: dict[str, Any] = field(default_factory=dict)
    candidate_asset_ids: list[str] = field(default_factory=list)
    approval_state: str = "not_required"
    checkpoint_type: str = "none"
    checkpoint_state: str = "none"
    checkpoint_payload: dict[str, Any] = field(default_factory=dict)
    clarification_status: str = "not_requested"
    clarification_turns: int = 0
    clarification_transcript: list[dict[str, Any]] = field(default_factory=list)
    resolved_task: str = ""
    resolved_slots: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task": self.task,
            "mode": self.mode,
            "status": self.status.value,
            "pipeline": self.pipeline,
            "result": self.result,
            "error": self.error,
            "parent_asset_id": self.parent_asset_id,
            "attempt_count": self.attempt_count,
            "final_score": self.final_score,
            "capability_gap": self.capability_gap,
            "candidate_asset_ids": self.candidate_asset_ids,
            "approval_state": self.approval_state,
            "checkpoint_type": self.checkpoint_type,
            "checkpoint_state": self.checkpoint_state,
            "checkpoint_payload": self.checkpoint_payload,
            "clarification_status": self.clarification_status,
            "clarification_turns": self.clarification_turns,
            "clarification_transcript": self.clarification_transcript,
            "resolved_task": self.resolved_task,
            "resolved_slots": self.resolved_slots,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        return cls(
            job_id=data["job_id"],
            task=data["task"],
            mode=data.get("mode", "run"),
            status=JobStatus(data.get("status", JobStatus.PENDING)),
            pipeline=data.get("pipeline", ""),
            result=data.get("result", {}),
            error=data.get("error", ""),
            parent_asset_id=data.get("parent_asset_id", ""),
            attempt_count=data.get("attempt_count", 0),
            final_score=float(data.get("final_score", 0.0)),
            capability_gap=data.get("capability_gap", {}),
            candidate_asset_ids=list(data.get("candidate_asset_ids", [])),
            approval_state=data.get("approval_state", "not_required"),
            checkpoint_type=data.get("checkpoint_type", "none"),
            checkpoint_state=data.get("checkpoint_state", "none"),
            checkpoint_payload=dict(data.get("checkpoint_payload", {})),
            clarification_status=data.get("clarification_status", "not_requested"),
            clarification_turns=int(data.get("clarification_turns", 0)),
            clarification_transcript=list(data.get("clarification_transcript", [])),
            resolved_task=data.get("resolved_task", ""),
            resolved_slots=dict(data.get("resolved_slots", {})),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
        )


class JobManager:
    def __init__(self, jobs_dir: str | Path = ".jobs") -> None:
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create_job(self, task: str, mode: str = "run", parent_asset_id: str = "") -> Job:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = Job(job_id=job_id, task=task, mode=mode, parent_asset_id=parent_asset_id)
        self._save_job(job)
        return job

    def get_job(self, job_id: str) -> Job | None:
        job_file = self.jobs_dir / f"{job_id}.json"
        if not job_file.exists():
            return None

        with open(job_file, "r") as f:
            data = json.load(f)

        return Job.from_dict(data)

    def update_status(self, job_id: str, status: JobStatus) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.status = status
        if status == JobStatus.RUNNING and not job.started_at:
            job.started_at = datetime.utcnow().isoformat() + "Z"
        elif status in (JobStatus.COMPLETED, JobStatus.FAILED):
            job.completed_at = datetime.utcnow().isoformat() + "Z"

        self._save_job(job)

    def update_pipeline(self, job_id: str, pipeline: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.pipeline = pipeline
        self._save_job(job)

    def update_result(self, job_id: str, result: dict[str, Any]) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.result = result
        self._save_job(job)

    def update_attempts(
        self,
        job_id: str,
        attempt_count: int,
        final_score: float | None = None,
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.attempt_count = attempt_count
        if final_score is not None:
            job.final_score = final_score
        self._save_job(job)

    def update_capability_gap(self, job_id: str, capability_gap: dict[str, Any]) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.capability_gap = capability_gap
        self._save_job(job)

    def add_candidate_asset(self, job_id: str, candidate_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        if candidate_id not in job.candidate_asset_ids:
            job.candidate_asset_ids.append(candidate_id)
        self._save_job(job)

    def update_approval_state(self, job_id: str, approval_state: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.approval_state = approval_state
        self._save_job(job)

    def update_checkpoint(
        self,
        job_id: str,
        *,
        checkpoint_type: str,
        checkpoint_state: str,
        checkpoint_payload: dict[str, Any] | None = None,
        status: JobStatus | None = None,
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.checkpoint_type = checkpoint_type
        job.checkpoint_state = checkpoint_state
        job.checkpoint_payload = checkpoint_payload or {}
        if status is not None:
            job.status = status
            if status == JobStatus.RUNNING and not job.started_at:
                job.started_at = datetime.utcnow().isoformat() + "Z"
        self._save_job(job)

    def update_clarification(
        self,
        job_id: str,
        *,
        status: str,
        turns: int,
        transcript: list[dict[str, Any]],
        resolved_task: str,
        resolved_slots: dict[str, Any],
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.clarification_status = status
        job.clarification_turns = turns
        job.clarification_transcript = transcript
        job.resolved_task = resolved_task
        job.resolved_slots = resolved_slots
        self._save_job(job)

    def update_error(self, job_id: str, error: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        job.error = error
        job.status = JobStatus.FAILED
        job.completed_at = datetime.utcnow().isoformat() + "Z"
        self._save_job(job)

    def list_jobs(self) -> list[Job]:
        jobs = []
        for job_file in self.jobs_dir.glob("*.json"):
            with open(job_file, "r") as f:
                data = json.load(f)
            jobs.append(Job.from_dict(data))

        # Sort by created_at, newest first
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def _save_job(self, job: Job) -> None:
        job_file = self.jobs_dir / f"{job.job_id}.json"
        with open(job_file, "w") as f:
            json.dump(job.to_dict(), f, indent=2)

    def delete_job(self, job_id: str) -> bool:
        job_file = self.jobs_dir / f"{job_id}.json"
        if job_file.exists():
            job_file.unlink()
            return True
        return False
