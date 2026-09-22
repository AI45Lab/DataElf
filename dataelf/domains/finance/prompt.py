from __future__ import annotations

import json

from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob
from .artifacts import OutputSchema, json_schema, output_schema_for_spec
from .config import CODE_TOOLS, EDGAR_TOOLS, MARKET_TOOLS, SQL_TOOLS, TERMINAL_TOOLS, WEB_TOOLS


def build_finance_prompt(job: DiscoveryJob, context: DiscoveryContext) -> str:
    """Compose the generic Finance method with benchmark sections.

    The domain owns the composition, safety rules, and a single exploration
    method spanning convergent answering and open synthesis: the model judges
    the depth the objective calls for instead of being routed by a mode
    label. The concrete deliverable (path, payload shape, required fields) is
    rendered from the resolved output schema in ``artifacts.py`` — the same
    single source that drives the output contract, review validation, and
    result-id extraction. A selected benchmark contributes task vocabulary,
    source description, tool guidance, and output requirements through
    ``parameters.prompt_profile``; web-first or data-first emphasis is
    derived from the effective tool set, not from a method mode.
    """
    schema = output_schema_for_spec(job.spec.parameters)
    profile = normalize_prompt_profile(job.spec.parameters.get("prompt_profile"))
    tools = tuple(job.spec.parameters.get("finance_tool_names", []) or [])
    objective = job.spec.objective
    profile_name = _text(profile.get("name"), "generic finance analysis")
    source_guidance = _text(
        profile.get("source_guidance"),
        "Use the prepared source artifacts listed by the runtime. Do not access data outside the authorized workspace.",
    )
    tool_guidance = _text(
        profile.get("tool_guidance"),
        "Use only the tools listed below. Respect each tool's schema, read/write restrictions, and execution errors.",
    )
    output_guidance = _text(
        profile.get("output_guidance"),
        "Produce the declared Finance output artifacts and attach evidence references to every supported claim.",
    )
    stopping_policy = _text(profile.get("stopping_policy"), "")
    profile_instructions = _bullet_lines(profile.get("instructions"))
    extra_requirements = _bullet_lines(profile.get("insight_requirements"))
    return f"""You are a finance data research agent.

## Analysis profile
The selected analysis profile is `{profile_name}`. The profile is supplied by the benchmark configuration and may specialize the source, tools, output format, or stopping policy. Follow it in addition to the general Finance method below.

## Data and tools
{source_guidance}
Available tools in this run: {', '.join(tools) if tools else 'none beyond the runtime defaults'}.
{tool_guidance}
{_section("Profile instructions", profile_instructions)}

## Exploration method
{_unified_method()}

## Depth and stopping
Calibrate your own depth: for objectives that pin down a definitive answer, stop when the answer is verified and complete; for open synthesis, stop when additional evidence stops changing the conclusions or the tool budget is exhausted. Never loop or invent facts. Apply any profile-specific stopping policy supplied below.
{_section("Profile stopping policy", [stopping_policy] if stopping_policy else [])}

## Finding requirements
{schema.requirement_prose}
{_section("Profile finding requirements", extra_requirements)}

## Output
{output_guidance}

{_deliverable(schema)}

User objective: {objective}
"""


def _unified_method() -> str:
    return """1. Restate the objective and judge what it calls for — a definitive answer that must be pinned down, an open synthesis across many angles, or a mix. Let the objective and the declared deliverable decide; do not wait to be told which strategy to run.
2. Inspect the prepared source artifacts, metadata, and schemas before making assumptions.
3. For a definitive answer: plan the shortest sufficient evidence chain, resolve periods, units, definitions, and entities explicitly, and cross-check against an independent source or derivation when the tools allow. For an open synthesis: form several candidate hypotheses across growth, profitability, capital structure, reporting quality, segments, anomalies, valuation, risk, or other areas relevant to the objective, and test them; check denominators, units, reporting periods, duplicate records, selection bias, and alternative explanations.
4. Calibrate your own stopping: stop when the answer is verified and complete, or when additional evidence stops changing the conclusions or the tool budget is exhausted — then synthesize.
5. Keep a compact evidence trail in the declared workspace directories and write reusable analysis scripts when calculations are non-trivial.
6. Deliver exactly the declared output artifacts with evidence references attached to every supported claim."""


def _deliverable(schema: OutputSchema) -> str:
    lines = [
        "Declared deliverable:",
        f"- `{schema.path}` — {schema.deliverable_description}, conforming to:",
        "```json",
        json.dumps(json_schema(schema), indent=2, ensure_ascii=False),
        "```",
        "  Additional fields beyond this contract are welcome when they add value (per-finding evidence, methodology, data notes).",
        f"- `{schema.brief_path}` — the human-readable synthesis in free Markdown structure.",
    ]
    return "\n".join(lines)


def tool_names_for_flags(flags: dict[str, bool], extra: list[str] | None = None) -> tuple[str, ...]:
    """Deterministic ordered tool listing shared by the prompt and the plugin."""
    groups = (
        (SQL_TOOLS, "sql"),
        (CODE_TOOLS, "python"),
        (EDGAR_TOOLS, "edgar"),
        (MARKET_TOOLS, "prices"),
        (WEB_TOOLS, "web"),
    )
    names: list[str] = []
    for tools, flag in groups:
        if flags.get(flag):
            names.extend(tools)
    # Pi's built-in terminal tools ship with the explorer and are always
    # available; list them unconditionally so "use only the tools listed"
    # matches reality.
    names.extend(TERMINAL_TOOLS)
    names.extend(extra or [])
    return tuple(dict.fromkeys(names))


_PROMPT_PROFILE_FIELDS = {
    "profile_id", "name", "version", "source_guidance", "tool_guidance",
    "instructions", "insight_requirements", "output_guidance", "stopping_policy",
}


def normalize_prompt_profile(value: object) -> dict[str, object]:
    """Validate the benchmark-provided profile before it reaches the model prompt."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("finance prompt_profile must be an object")
    unknown = sorted(set(value) - _PROMPT_PROFILE_FIELDS)
    if unknown:
        raise ValueError("Unknown finance prompt_profile fields: " + ", ".join(unknown))
    result = dict(value)
    for field in ("profile_id", "name", "version", "source_guidance", "tool_guidance", "output_guidance", "stopping_policy"):
        if field in result and not isinstance(result[field], str):
            raise ValueError(f"finance prompt_profile.{field} must be a string")
    for field in ("instructions", "insight_requirements"):
        if field in result:
            if not isinstance(result[field], list):
                raise ValueError(f"finance prompt_profile.{field} must be a list")
            if not all(isinstance(item, str) for item in result[field]):
                raise ValueError(f"finance prompt_profile.{field} items must be strings")
    return result


def _text(value: object, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _bullet_lines(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n## " + title + "\n" + "\n".join(f"- {line}" for line in lines) + "\n"


__all__ = ["build_finance_prompt", "normalize_prompt_profile", "tool_names_for_flags"]
