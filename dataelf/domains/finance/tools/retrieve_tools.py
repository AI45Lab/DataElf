"""retrieve_information: expand stored filing text into a bounded prompt.

Follows FinanceAgentBench placeholder/range semantics with the result limits
from TOOL_RESULT_LIMITS_PLAN.md: each {{key}} expansion is capped at
RETRIEVE_KEY_CHARS_MAX characters (a range must be supplied for larger
documents) and the fully expanded prompt at RETRIEVE_PROMPT_CHARS_MAX.
Oversized requests fail with actionable errors instead of silently dropping
document content.
"""

from __future__ import annotations

import re
from typing import Any

from .limits import RETRIEVE_KEY_CHARS_MAX, RETRIEVE_PROMPT_CHARS_MAX
from .workspace import storage_path, workspace_root


def retrieve_information(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "")
    if not prompt:
        raise ValueError("prompt is required")
    keys = re.findall(r"{{([^{}]+)}}", prompt)
    if not keys:
        raise ValueError("prompt must include at least one stored key in the form {{key_name}}")
    raw_ranges = args.get("input_character_ranges") or []
    if not isinstance(raw_ranges, list):
        raise ValueError("input_character_ranges must be a list")
    ranges: dict[str, tuple[int, int]] = {}
    for item in raw_ranges:
        if not isinstance(item, dict) or not {"key", "start", "end"} <= set(item):
            raise ValueError("each input_character_ranges item needs key, start, and end")
        key = str(item["key"])
        if key not in keys:
            raise ValueError(f"range key '{key}' is not referenced in prompt")
        try:
            start, end = int(item["start"]), int(item["end"])
        except (TypeError, ValueError):
            raise ValueError(f"range for '{key}' must use integer start/end") from None
        if start < 0 or end < start:
            raise ValueError(f"range for '{key}' must satisfy 0 <= start <= end")
        ranges[key] = (start, end)
    documents: dict[str, str] = {}
    stored_lengths: dict[str, int] = {}
    applied_ranges: dict[str, tuple[int, int]] = {}
    range_clipped = False
    for key in dict.fromkeys(keys):
        path = storage_path(key)
        if not path.is_file():
            raise ValueError(f"stored key '{key}' was not found; call parse_html_page with key='{key}' first")
        content = path.read_text(encoding="utf-8")
        stored_lengths[key] = len(content)
        if key in ranges:
            start, end = ranges[key]
            start, end = min(start, len(content)), min(end, len(content))
            if (start, end) != ranges[key]:
                range_clipped = True
            if end - start > RETRIEVE_KEY_CHARS_MAX:
                raise ValueError(
                    f"range for '{key}' spans {end - start} characters, above the "
                    f"{RETRIEVE_KEY_CHARS_MAX}-character per-key limit; split the document into "
                    "multiple retrieve_information calls with smaller input_character_ranges"
                )
            applied_ranges[key] = (start, end)
            content = content[start:end]
        elif len(content) > RETRIEVE_KEY_CHARS_MAX:
            raise ValueError(
                "stored key '" + key + f"' holds {len(content)} characters, above the "
                f"{RETRIEVE_KEY_CHARS_MAX}-character per-key limit; provide input_character_ranges "
                "for '{{" + key + "}}' to read a bounded window of the document"
            )
        documents[key] = content
    expanded = re.sub(r"{{([^{}]+)}}", lambda match: documents[match.group(1)], prompt)
    if len(expanded) > RETRIEVE_PROMPT_CHARS_MAX:
        raise ValueError(
            f"expanded prompt is {len(expanded)} characters, above the "
            f"{RETRIEVE_PROMPT_CHARS_MAX}-character limit; narrow the prompt text or shrink the "
            "input_character_ranges windows and re-issue the call"
        )
    return {
        "prompt": expanded,
        "keys": list(dict.fromkeys(keys)),
        "prompt_length": len(expanded),
        "truncated": range_clipped,
        "stored_lengths": stored_lengths,
        "character_ranges": {key: [start, end] for key, (start, end) in applied_ranges.items()},
        "stored_paths": {key: str(storage_path(key).relative_to(workspace_root())) for key in dict.fromkeys(keys)},
    }
