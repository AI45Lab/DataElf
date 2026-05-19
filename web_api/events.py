from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterator


class JobEventBus:
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._next_ids: dict[str, int] = defaultdict(lambda: 1)
        self._condition = threading.Condition()

    def publish(self, job_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            event_id = self._next_ids[job_id]
            self._next_ids[job_id] += 1
            stored = dict(event)
            stored.setdefault("job_id", job_id)
            stored["event_id"] = event_id
            stored.setdefault("timestamp", datetime.utcnow().isoformat() + "Z")
            self._events[job_id].append(stored)
            self._condition.notify_all()
            return dict(stored)

    def replay(self, job_id: str, after_event_id: int | None = None) -> list[dict[str, Any]]:
        with self._condition:
            events = list(self._events.get(job_id, []))
        if after_event_id is None:
            return [dict(event) for event in events]
        return [dict(event) for event in events if int(event.get("event_id", 0)) > after_event_id]

    def subscribe(
        self,
        job_id: str,
        after_event_id: int | None = None,
        *,
        heartbeat_seconds: float = 15.0,
    ) -> Iterator[dict[str, Any]]:
        last_event_id = after_event_id or 0
        while True:
            with self._condition:
                pending = [
                    event
                    for event in self._events.get(job_id, [])
                    if int(event.get("event_id", 0)) > last_event_id
                ]
                if not pending:
                    self._condition.wait(timeout=heartbeat_seconds)
                    pending = [
                        event
                        for event in self._events.get(job_id, [])
                        if int(event.get("event_id", 0)) > last_event_id
                    ]
            if not pending:
                yield {"type": "heartbeat", "job_id": job_id}
                continue
            for event in pending:
                last_event_id = int(event.get("event_id", last_event_id))
                yield dict(event)
                if event.get("type") in {"job.completed", "job.failed", "job.unsupported"}:
                    return

    def sse_lines(self, job_id: str, after_event_id: int | None = None) -> Iterator[str]:
        for event in self.subscribe(job_id, after_event_id=after_event_id):
            if event.get("type") == "heartbeat":
                yield ": heartbeat\n\n"
                continue
            yield f"id: {event['event_id']}\n"
            yield f"event: {event.get('type', 'message')}\n"
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
