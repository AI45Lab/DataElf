"""Credential-safe diagnostics without coupling core to an entrypoint."""
from __future__ import annotations
import os
import re
from collections.abc import Mapping

MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION")


def secret_values(values):
    result = set()
    if isinstance(values, Mapping):
        for key, value in values.items():
            if isinstance(value, Mapping):
                result.update(secret_values(value))
            elif isinstance(value, str) and value and any(marker in str(key).upper() for marker in MARKERS):
                result.add(value)
    return result


def redact_text(value, secrets=()):
    text = str(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "<redacted>")
    return re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|ak_[A-Za-z0-9]{8,})", "<redacted>", text)


def redact_data(value, secrets=()):
    if isinstance(value, Mapping):
        return {key: ("<redacted>" if any(marker in str(key).upper() for marker in MARKERS) else redact_data(item, secrets)) for key, item in value.items() if str(key).lower() != "env"}
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]
    return redact_text(value, secrets) if isinstance(value, str) else value
