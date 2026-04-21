"""Shared regex pattern scanner for rule-based checkers.

Loads pattern definitions from resources/patterns/*.json and provides
scan_patterns() for text scanning with context extraction.
"""
import json
import os
import re
from typing import Any, Dict, List

_PATTERNS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "resources", "patterns")
)


def load_patterns(filename: str, log=None) -> List[Dict[str, Any]]:
    """Load and compile regex patterns from a JSON file in resources/patterns/.

    Each entry in the returned list contains the original fields plus:
      - "compiled": pre-compiled re.Pattern
    """
    import logging
    _log = log or logging.getLogger(__name__)

    path = os.path.join(_PATTERNS_DIR, filename)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    result = []
    for p in data.get("patterns", []):
        try:
            compiled = re.compile(p["pattern"])
        except re.error as e:
            _log.warning(f"Skipping invalid regex pattern {p.get('name', '?')!r} in {filename}: {e}")
            continue
        result.append({**p, "compiled": compiled})
    return result


def scan_patterns(text: str, patterns: List[Dict[str, Any]], field_name: str) -> List[dict]:
    """Scan text against compiled patterns and return hit records.

    Each hit contains:
      field_name, pattern_name, display_name, start, end, matched, context
    The 'validator' field (if present) is passed through for the caller to apply.
    """
    hits = []
    for pat in patterns:
        for m in pat["compiled"].finditer(text):
            ctx_start = max(0, m.start() - 40)
            ctx_end = min(len(text), m.end() + 40)
            prefix = "..." if ctx_start > 0 else ""
            suffix = "..." if ctx_end < len(text) else ""
            context = prefix + text[ctx_start:ctx_end] + suffix
            hits.append({
                "field_name": field_name,
                "pattern_name": pat["name"],
                "display_name": pat.get("display_name", pat["name"]),
                "validator": pat.get("validator"),
                "start": m.start(),
                "end": m.end(),
                "matched": m.group(),
                "context": context,
            })
    return hits
