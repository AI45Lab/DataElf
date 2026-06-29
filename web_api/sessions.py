from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


SESSION_TERMINAL_STATUSES = {"completed", "failed"}


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


@dataclass
class WebSession:
    session_id: str
    name: str = "Untitled"
    mode: str | None = None
    backend_mode: str | None = None
    job_id: str | None = None
    status: str = "new"
    locked: bool = False
    snapshot: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        now = _utc_now()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "mode": self.mode,
            "backend_mode": self.backend_mode,
            "job_id": self.job_id,
            "status": self.status,
            "locked": self.locked,
            "snapshot": self.snapshot,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebSession":
        return cls(
            session_id=str(data["session_id"]),
            name=str(data.get("name") or "Untitled"),
            mode=data.get("mode"),
            backend_mode=data.get("backend_mode"),
            job_id=data.get("job_id"),
            status=str(data.get("status") or "new"),
            locked=bool(data.get("locked", False)),
            snapshot=dict(data.get("snapshot") or {}),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class WebSessionManager:
    def __init__(self, sessions_dir: str | Path = ".web_sessions") -> None:
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create_session(self, name: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = WebSession(
                session_id=f"sess_{uuid.uuid4().hex[:12]}",
                name=(name or "Untitled").strip() or "Untitled",
            )
            self._save_session(session)
            return session.to_dict()

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = []
        with self._lock:
            for session_file in self.sessions_dir.glob("*.json"):
                session = self._read_session_file(session_file)
                if session is not None:
                    sessions.append(session)
        sessions.sort(key=lambda item: item.created_at, reverse=True)
        return [session.to_dict() for session in sessions]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._load_session(session_id)
            return None if session is None else session.to_dict()

    def update_session(
        self,
        session_id: str,
        *,
        name: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            if name is not None:
                session.name = name.strip() or "Untitled"
            session.updated_at = _utc_now()
            self._save_session(session)
            return session.to_dict()

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            path = self._session_path(session_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    def set_mode(self, session_id: str, mode: str) -> dict[str, Any] | None:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"run", "pilot"}:
            raise ValueError("mode must be 'run' or 'pilot'")

        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            if session.job_id:
                raise RuntimeError("Cannot change mode after a job has been bound.")
            if session.locked:
                raise RuntimeError("Cannot change mode for a locked session.")
            session.mode = normalized_mode
            session.backend_mode = normalized_mode
            session.status = "mode_selected"
            session.updated_at = _utc_now()
            self._save_session(session)
            return session.to_dict()

    def bind_job(
        self,
        session_id: str,
        job_id: str,
        *,
        status: str = "running",
    ) -> dict[str, Any] | None:
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            if session.job_id and session.job_id != job_id:
                raise RuntimeError("Session already has a bound job.")
            session.job_id = job_id
            session.status = status
            session.locked = status in SESSION_TERMINAL_STATUSES
            session.updated_at = _utc_now()
            self._save_session(session)
            return session.to_dict()

    def update_snapshot(
        self,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            session.snapshot = dict(snapshot or {})
            session.updated_at = _utc_now()
            self._save_session(session)
            return session.to_dict()

    def complete_for_job(self, job_id: str, status: str) -> dict[str, Any] | None:
        normalized_status = "completed" if status == "completed" else "failed"
        with self._lock:
            session = self._find_by_job_id(job_id)
            if session is None:
                return None
            session.status = normalized_status
            session.locked = True
            session.updated_at = _utc_now()
            self._save_session(session)
            return session.to_dict()

    def _session_path(self, session_id: str) -> Path:
        if "/" in session_id or "\\" in session_id:
            raise ValueError("Invalid session_id")
        return self.sessions_dir / f"{session_id}.json"

    def _load_session(self, session_id: str) -> WebSession | None:
        path = self._session_path(session_id)
        if not path.exists():
            return None
        return self._read_session_file(path)

    def _find_by_job_id(self, job_id: str) -> WebSession | None:
        for session_file in self.sessions_dir.glob("*.json"):
            session = self._read_session_file(session_file)
            if session is not None and session.job_id == job_id:
                return session
        return None

    def _read_session_file(self, path: Path) -> WebSession | None:
        try:
            with open(path, "r", encoding="utf-8") as file:
                return WebSession.from_dict(json.load(file))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_session(self, session: WebSession) -> None:
        path = self._session_path(session.session_id)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(session.to_dict(), file, indent=2, sort_keys=True)
