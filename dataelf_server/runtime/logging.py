"""Service diagnostics in the server state directory."""
from __future__ import annotations
import logging
import os
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from dataelf.discovery.redaction import redact_text, secret_values


@contextmanager
def service_logging(settings):
    secrets = secret_values(settings.core.model_dump()) | secret_values(os.environ)
    class Formatter(logging.Formatter):
        def format(self, record):
            return redact_text(super().format(record), secrets)
    directory = settings.state_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "server.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    loggers = [logging.getLogger(name) for name in ("dataelf.discovery", "uvicorn.error", "uvicorn.access")]
    previous_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        for logger, level in zip(loggers, previous_levels):
            logger.removeHandler(handler)
            logger.setLevel(level)
        handler.close()
