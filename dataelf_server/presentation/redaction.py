from __future__ import annotations

import re


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[0-9A-Za-z_-]+"),
    re.compile(r"\bak_[0-9A-Za-z]+"),
    re.compile(r"(?i)(api[_-]?key[=:]\s*)[^\s,;]+"),
)


def redact_message(message: object, limit: int = 1200) -> str:
    rendered = str(message)
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            rendered,
        )
    return rendered[:limit]

