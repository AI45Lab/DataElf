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


def append_dataset_to_task(task: str, dataset_name: str) -> str:
    return f"{task.rstrip()}\n\nUse dataset {dataset_name} as dataset_name."


def _contains_dataset_name(task: str, dataset_name: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(dataset_name)}(?![A-Za-z0-9_])"
    return re.search(pattern, task) is not None


def _default_dataset_name(options: list[str]) -> str:
    if "security_audit_samples" in options:
        return "security_audit_samples"
    return options[0] if options else ""
