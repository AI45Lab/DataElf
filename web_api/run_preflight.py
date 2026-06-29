from __future__ import annotations

import re
from typing import Any


def find_dataset_mentions(task: str, dataset_schemas: dict[str, list[str]]) -> list[str]:
    mentions: list[str] = []
    for dataset_name in sorted(dataset_schemas, key=len, reverse=True):
        if _contains_dataset_name(task, dataset_name):
            mentions.append(dataset_name)
    return mentions


def needs_dataset_selection(task: str, dataset_schemas: dict[str, list[str]]) -> bool:
    if not dataset_schemas:
        return False
    return not find_dataset_mentions(task, dataset_schemas)


def build_dataset_selection_payload(dataset_schemas: dict[str, list[str]]) -> dict[str, Any]:
    options = sorted(dataset_schemas)
    default_dataset = _default_dataset_name(options)
    return {
        "checkpoint_type": "dataset_selection",
        "payload": {
            "prompt": (
                "请选择要运行的数据集。输入 default 使用推荐数据集，"
                "或直接输入一个数据集名称。"
            ),
            "options": options,
            "suggested_defaults": {"dataset_name": default_dataset} if default_dataset else {},
            "missing_items": ["dataset_name"],
            "response_mode": "select_one",
        },
    }


def resolve_dataset_answer(answer: str, dataset_schemas: dict[str, list[str]]) -> str:
    options = sorted(dataset_schemas)
    if not options:
        raise ValueError("No datasets are available.")

    normalized = answer.strip()
    normalized_lower = normalized.lower()
    if normalized_lower in {"default", "use default", "use defaults", "ok", "okay", "yes", "y", "好", "可以", "是"}:
        default_dataset = _default_dataset_name(options)
        if default_dataset:
            return default_dataset

    for dataset_name in options:
        if normalized == dataset_name:
            return dataset_name

    comma_first = normalized.split(",", 1)[0].strip()
    for dataset_name in options:
        if comma_first == dataset_name:
            return dataset_name

    raise ValueError(
        f"Unknown dataset '{answer}'. Available datasets: {', '.join(options)}"
    )


def find_tool_mentions(task: str, tool_schemas: list[dict[str, Any]]) -> list[str]:
    mentions: list[str] = []
    for tool_name in sorted(_tool_names(tool_schemas), key=len, reverse=True):
        if _contains_tool_name(task, tool_name):
            mentions.append(tool_name)
    return mentions


def needs_tool_selection(task: str, tool_schemas: list[dict[str, Any]]) -> bool:
    if not tool_schemas:
        return False
    return not find_tool_mentions(task, tool_schemas)


def build_tool_selection_payload(tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
    options = sorted(_tool_names(tool_schemas))
    default_tool = _default_tool_name(options)
    return {
        "checkpoint_type": "tool_selection",
        "payload": {
            "prompt": (
                "请选择要运行的工具。输入 default 使用推荐工具，"
                "或直接输入一个工具名称。"
            ),
            "options": options,
            "suggested_defaults": {"tool_name": default_tool} if default_tool else {},
            "missing_items": ["tool_name"],
            "response_mode": "select_one",
        },
    }


def resolve_tool_answer(answer: str, tool_schemas: list[dict[str, Any]]) -> str:
    options = sorted(_tool_names(tool_schemas))
    if not options:
        raise ValueError("No tools are available.")

    normalized = answer.strip()
    normalized_lower = normalized.lower()
    if normalized_lower in {"default", "use default", "use defaults", "ok", "okay", "yes", "y", "好", "可以", "是"}:
        default_tool = _default_tool_name(options)
        if default_tool:
            return default_tool

    for tool_name in options:
        if normalized == tool_name:
            return tool_name

    comma_first = normalized.split(",", 1)[0].strip()
    for tool_name in options:
        if comma_first == tool_name:
            return tool_name

    raise ValueError(
        f"Unknown tool '{answer}'. Available tools: {', '.join(options)}"
    )


def append_dataset_to_task(task: str, dataset_name: str) -> str:
    task_text = task.rstrip()
    suffix = f"Use dataset {dataset_name} as dataset_name."
    return f"{task_text}\n\n{suffix}" if task_text else suffix


def append_tool_to_task(task: str, tool_name: str) -> str:
    task_text = task.strip()
    if not task_text:
        return f"run {tool_name}"
    if task_text.startswith("Use dataset "):
        return f"run {tool_name}\n\n{task_text}"
    return f"{task_text}\n\nUse tool {tool_name} as tool_name."


def _contains_dataset_name(task: str, dataset_name: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(dataset_name)}(?![A-Za-z0-9_])"
    return re.search(pattern, task) is not None


def _contains_tool_name(task: str, tool_name: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])"
    return re.search(pattern, task) is not None


def _default_dataset_name(options: list[str]) -> str:
    if "security_audit_samples" in options:
        return "security_audit_samples"
    return options[0] if options else ""


def _default_tool_name(options: list[str]) -> str:
    if "security_audit" in options:
        return "security_audit"
    return options[0] if options else ""


def _tool_names(tool_schemas: list[dict[str, Any]]) -> list[str]:
    return [
        str(schema.get("name"))
        for schema in tool_schemas
        if isinstance(schema, dict) and schema.get("name")
    ]
