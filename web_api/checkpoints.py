from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class WebCheckpoint:
    job_id: str
    checkpoint_id: str
    checkpoint_type: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    answer: dict[str, Any] | None = None


class WebCheckpointBroker:
    def __init__(self) -> None:
        self._checkpoints: dict[tuple[str, str], WebCheckpoint] = {}
        self._condition = threading.Condition()

    def create_checkpoint(
        self,
        *,
        job_id: str,
        checkpoint_type: str,
        payload: dict[str, Any],
    ) -> WebCheckpoint:
        checkpoint = WebCheckpoint(
            job_id=job_id,
            checkpoint_id=f"chk_{uuid.uuid4().hex[:12]}",
            checkpoint_type=checkpoint_type,
            payload=dict(payload),
        )
        with self._condition:
            self._checkpoints[(job_id, checkpoint.checkpoint_id)] = checkpoint
            self._condition.notify_all()
        return checkpoint

    def get_checkpoint(self, job_id: str, checkpoint_id: str) -> WebCheckpoint | None:
        with self._condition:
            checkpoint = self._checkpoints.get((job_id, checkpoint_id))
            return None if checkpoint is None else WebCheckpoint(**checkpoint.__dict__)

    def answer_checkpoint(
        self,
        *,
        job_id: str,
        checkpoint_id: str,
        answer: dict[str, Any],
    ) -> bool:
        with self._condition:
            checkpoint = self._checkpoints.get((job_id, checkpoint_id))
            if checkpoint is None:
                return False
            checkpoint.answer = dict(answer)
            self._condition.notify_all()
            return True

    def wait_for_answer(
        self,
        *,
        job_id: str,
        checkpoint_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        with self._condition:
            checkpoint = self._checkpoints.get((job_id, checkpoint_id))
            if checkpoint is None:
                raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
            if checkpoint.answer is None:
                self._condition.wait_for(
                    lambda: checkpoint.answer is not None,
                    timeout=timeout_seconds,
                )
            if checkpoint.answer is None:
                return {"decision": "defer", "reason": "Timed out waiting for checkpoint answer."}
            return dict(checkpoint.answer)
