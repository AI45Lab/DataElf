from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from config import Config


class LogLevel(str, Enum):

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class LogEntry:

    job_id: str
    step: str
    level: str
    message: str
    timestamp: str
    duration_seconds: float | None = None
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "job_id": self.job_id,
            "step": self.step,
            "level": self.level,
            "message": self.message,
            "timestamp": self.timestamp,
            "duration_ms": round(self.duration_seconds * 1000, 1) if self.duration_seconds is not None else 0,
            "extra": self.extra or {},
        }
        return result


class JobLogger:

    def __init__(
        self,
        job_id: str,
        enable_console: bool = True,
        database: Optional[Any] = None,
        log_level: str = "INFO",
        entry_handler: Callable[[dict[str, Any]], None] | None = None,
    ):

        self.job_id = job_id
        self.enable_console = enable_console
        self.database = database
        self.step_counter = 0
        self._last_step_start: datetime | None = None
        self.entries: list[dict[str, Any]] = []
        self.entry_handler = entry_handler

        # Setup console logger
        self.console_logger = logging.getLogger(f"pilot.{job_id}")
        self.console_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        self.console_logger.handlers.clear()

        if enable_console:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.console_logger.addHandler(handler)

    def _get_step_name(self) -> str:
        self.step_counter += 1
        return f"step_{self.step_counter}"

    def _log(
        self,
        level: LogLevel,
        message: str,
        step: Optional[str] = None,
        extra: dict[str, Any] | None = None,
        save_to_db: bool = True,
    ) -> LogEntry:
        step_name = step or self._get_step_name()
        now = datetime.utcnow()
        timestamp = now.isoformat() + "Z"

        # Compute duration for the previous step (if any)
        prev_duration: float | None = None
        if self._last_step_start is not None:
            prev_duration = (now - self._last_step_start).total_seconds()

        entry = LogEntry(
            job_id=self.job_id,
            step=step_name,
            level=level.value,
            message=message,
            timestamp=timestamp,
            duration_seconds=prev_duration,
            extra=extra,
        )
        entry_dict = entry.to_dict()
        self.entries.append(entry_dict)
        self._notify_entry_handler(entry_dict)

        # Record start time for this step (used when next step begins)
        self._last_step_start = now

        # Console output
        if self.enable_console:
            log_func = getattr(self.console_logger, level.value.lower())
            duration_ms = round(prev_duration * 1000) if prev_duration is not None else 0
            icon = self._console_icon(level, message)
            prefix = f"{icon} " if icon else ""
            log_func(f"{prefix}[{step_name} · {duration_ms}ms] {message}")

        # Database storage
        if save_to_db and self.database:
            try:
                self.database.write_log(entry_dict)
            except Exception as e:
                self.console_logger.warning(f"Failed to write log to database: {e}")

        return entry

    def debug(self, message: str, **kwargs: Any) -> LogEntry:
        return self._log(LogLevel.DEBUG, message, extra=kwargs)

    def info(self, message: str, **kwargs: Any) -> LogEntry:
        return self._log(LogLevel.INFO, message, extra=kwargs)

    def warning(self, message: str, **kwargs: Any) -> LogEntry:
        return self._log(LogLevel.WARNING, message, extra=kwargs)

    def error(self, message: str, **kwargs: Any) -> LogEntry:
        return self._log(LogLevel.ERROR, message, extra=kwargs)

    def critical(self, message: str, **kwargs: Any) -> LogEntry:
        return self._log(LogLevel.CRITICAL, message, extra=kwargs)

    def log_step(self, message: str, **kwargs: Any) -> LogEntry:
        step = self._get_step_name()
        return self._log(LogLevel.INFO, message, step=step, extra=kwargs)

    def finish(self) -> None:
        """Mark job completion, recording the duration of the last active step."""
        if self._last_step_start is not None:
            end_time = datetime.utcnow()
            duration = (end_time - self._last_step_start).total_seconds()
            duration_ms = round(duration * 1000)
            entry = LogEntry(
                job_id=self.job_id,
                step="job_end",
                level=LogLevel.INFO.value,
                message="Job completed",
                timestamp=end_time.isoformat() + "Z",
                duration_seconds=duration,
                extra={"_job_end": True},
            )
            entry_dict = entry.to_dict()
            self.entries.append(entry_dict)
            self._notify_entry_handler(entry_dict)
            if self.enable_console:
                self.console_logger.info(f"🏆 [job_end · {duration_ms}ms] Job completed")
            if self.database:
                try:
                    self.database.write_log(entry_dict)
                except Exception as e:
                    self.console_logger.warning(f"Failed to write log to database: {e}")

    @property
    def log_ref(self) -> str | None:
        data_dir = getattr(self.database, "data_dir", None)
        if data_dir is None:
            return None
        return str(Path(data_dir).parent / ".logs" / f"{self.job_id}.json")

    def _notify_entry_handler(self, entry: dict[str, Any]) -> None:
        if self.entry_handler is None:
            return
        try:
            self.entry_handler(dict(entry))
        except Exception:
            return

    def _console_icon(self, level: LogLevel, message: str) -> str:
        message_lower = message.lower()
        if level in {LogLevel.ERROR, LogLevel.CRITICAL}:
            return "🔧"
        if level == LogLevel.WARNING:
            return "❓"
        if "completed" in message_lower or "success" in message_lower or "passed" in message_lower:
            return "✅"
        return ""


def get_logger(
    job_id: str,
    config: Optional[Config] = None,
    database: Optional[Any] = None,
    entry_handler: Callable[[dict[str, Any]], None] | None = None,
) -> JobLogger:
    enable_console = True
    log_level = "INFO"
    if config and hasattr(config, "execution"):
        enable_console = config.execution.enable_log
        log_level = getattr(config.execution, "log_level", "INFO")

    return JobLogger(
        job_id,
        enable_console=enable_console,
        database=database,
        log_level=log_level,
        entry_handler=entry_handler,
    )
