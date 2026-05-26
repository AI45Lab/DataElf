from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.execution_plan import PLAN_VERSION


DEFAULT_EXECUTION_PLAN_EXAMPLE = {
    "version": PLAN_VERSION,
    "steps": [
        {
            "id": "load_data",
            "op": "load_dataset",
            "dataset": "alpaca_data",
            "output": "data",
        },
        {
            "id": "score",
            "op": "invoke_skill",
            "skill": "data_scoring",
            "input": {"data": "$data", "scorer": "dataelf"},
            "output": "scored",
        },
        {
            "id": "select",
            "op": "invoke_skill",
            "skill": "data_select",
            "input": {
                "data": "$scored",
                "dataset_name": "alpaca_data",
                "budget": 500,
                "n_clusters": 100,
                "strategy": "proportional",
                "output_dir": "outputs/alpaca_data_500",
            },
            "output": "selected",
        },
        {"id": "save", "op": "save_result", "input": "$selected"},
    ],
}


EXECUTION_PLAN_SPECIFICATION = f"""
## Execution Plan

Return one JSON object with version `{PLAN_VERSION}`.

Allowed operations:

- `load_dataset`: load a configured dataset. Required fields: `id`, `op`, `dataset`, `output`. Optional fields: `filters`, `limit`, `columns`.
- `invoke_skill`: run one selected skill. Required fields: `id`, `op`, `skill`, `input`, `output`.
- `save_result`: save the final result. Required fields: `id`, `op`, `input`.
- `write_file`: write JSON data to a relative file path. Required fields: `id`, `op`, `input`, `path`.
- `write_db`: write records to a configured database table. Required fields: `id`, `op`, `input`, `table`.
- `log`: record progress. Required fields: `id`, `op`, `message`.

Variable references use `$name` and may only reference outputs from earlier steps.
"""


SKILL_USAGE_RULES = """
## Skill Usage Rules

1. Choose skills from the injected Available Skills list only.
2. Do not expose Python BaseTool classes, tool schemas, or Python DSL to the user.
3. The plan should normally be `load_dataset -> invoke_skill -> save_result` for dataset tasks.
4. Some skills, such as `enzyme_acquire` and `protein_analyzer`, may accept direct inline input and do not always require `load_dataset`.
5. If the user asks for data selection without specifying a scoring method, invoke `data_scoring` with `scorer=dataelf` before `data_select`.
6. Do not infer `security_audit` solely from a dataset name. Only choose it for explicit security, safety, privacy, risk, compliance, prompt-injection, jailbreak, toxicity, harmfulness, or bias requests.
7. Pass only inputs described by the selected skill planner view and user request.
8. Always include exactly one `save_result` step for the final answer.
"""


OUTPUT_FORMAT = """
Output valid JSON only.
Do not explain.
Do not include markdown fences.
"""


AGENT_ROLE = """
You are DataElf's skill-native execution planner.

Your task is to convert a user data task into a structured execution plan that DataElf can validate, trace, and run through skills.
"""


class PromptBuilder:
    def __init__(
        self,
        skill_views: list[dict[str, Any]] | None = None,
        dataset_schemas: dict[str, list[str]] | None = None,
        skill_docs: list[dict[str, str]] | None = None,
        execution_plan_example: dict[str, Any] | None = None,
    ) -> None:
        self.skill_views = skill_views or []
        self.dataset_schemas = dataset_schemas or {}
        self.skill_docs = skill_docs or []
        self.execution_plan_example = execution_plan_example or DEFAULT_EXECUTION_PLAN_EXAMPLE

    def build_messages(self, user_query: str) -> tuple[str, str]:
        sections = [
            AGENT_ROLE.strip(),
            EXECUTION_PLAN_SPECIFICATION.strip(),
            self._build_available_datasets_section(),
            SKILL_USAGE_RULES.strip(),
            self._build_available_skills_section(),
        ]
        docs = self._build_skill_docs_section()
        if docs:
            sections.append(docs)
        sections.extend([
            self._build_example_section(),
            f"## Output Format\n\n{OUTPUT_FORMAT.strip()}",
        ])
        return "\n\n".join(sections), f"## User Request\n\n{user_query}"

    def build_agent_prompt(self, user_query: str) -> str:
        system_prompt, user_prompt = self.build_messages(user_query)
        return system_prompt + "\n\n" + user_prompt

    def _build_available_datasets_section(self) -> str:
        if not self.dataset_schemas:
            return "## Available Datasets\n\nNo dataset schema information available."

        lines = [
            "## Available Datasets",
            "",
            "The following datasets are available via `load_dataset`. Use exact field names when filtering.",
            "",
        ]
        for table_name, fields in sorted(self.dataset_schemas.items()):
            lines.append(f"### `{table_name}`")
            lines.append(f"Fields: {', '.join(f'`{field}`' for field in fields)}")
            lines.append("")
        return "\n".join(lines).strip()

    def _build_available_skills_section(self) -> str:
        if not self.skill_views:
            return "## Available Skills\n\nNo skills available."

        lines = ["## Available Skills"]
        for view in self.skill_views:
            lines.append(f"\n### Skill: {view.get('name', 'unknown')}")
            lines.append(f"Description: {view.get('description', '')}")
            if view.get("usage_summary"):
                lines.append(f"Usage summary: {view['usage_summary']}")
            if view.get("clarification_hints"):
                lines.append(f"Clarification hints: {view['clarification_hints']}")
            allowed = view.get("allowed_tools") or []
            if allowed:
                lines.append(f"Allowed tools declared by skill: {', '.join(str(item) for item in allowed)}")
        return "\n".join(lines)

    def _build_skill_docs_section(self) -> str:
        if not self.skill_docs:
            return ""
        entries = []
        for entry in self.skill_docs:
            entries.append(f"### {entry['skill_name']}\n\n{entry['content']}")
        return "## Selected Skill Instructions\n\n" + "\n\n".join(entries)

    def _build_example_section(self) -> str:
        return (
            "## Execution Plan Example\n\n"
            + json.dumps(self.execution_plan_example, ensure_ascii=False, indent=2)
        )


def create_prompt_builder(
    skill_views: list[dict[str, Any]] | None = None,
    dataset_schemas: dict[str, list[str]] | None = None,
    skill_docs: list[dict[str, str]] | None = None,
) -> PromptBuilder:
    return PromptBuilder(
        skill_views=skill_views or [],
        dataset_schemas=dataset_schemas or {},
        skill_docs=skill_docs or [],
    )


def load_skill_doc_entries(
    skill_names: list[str],
    skill_root: str | Path,
    max_len: int = 2000,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    root = Path(skill_root)
    for skill_name in skill_names:
        skill_md = root / skill_name / "SKILL.md"
        if not skill_md.exists():
            continue
        entries.append({
            "skill_name": skill_name,
            "path": str(skill_md),
            "content": skill_md.read_text(encoding="utf-8")[:max_len].strip(),
            "kind": "skill",
        })
        references_dir = root / skill_name / "references"
        if references_dir.exists():
            for reference in sorted(references_dir.glob("*.md")):
                entries.append({
                    "skill_name": skill_name,
                    "path": str(reference),
                    "content": reference.read_text(encoding="utf-8")[:max_len].strip(),
                    "kind": "reference",
                })
    return entries


def build_agent_prompt(
    user_query: str,
    skill_views: list[dict[str, Any]],
) -> str:
    builder = create_prompt_builder(skill_views=skill_views)
    return builder.build_agent_prompt(user_query)
