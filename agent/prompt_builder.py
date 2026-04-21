# Prompt Builder for Agent Pipeline Generation

import re
from pathlib import Path
from typing import Any

# Default Pipeline Example (used in prompts)
DEFAULT_PIPELINE_EXAMPLE = '''log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step(f"Loaded {len(data)} records")

log_step("Scoring data quality")

scored = run_tool(
    "data_scoring",
    data=data,
    scorer="dataelf"
)

log_step(f"Scored {len(scored)} records")

log_step("Running diversity-aware selection")

selected = run_tool(
    "data_select",
    data=scored,
    dataset_name="alpaca_data",
    budget=500,
    n_clusters=100,
    strategy="proportional",
    output_dir="outputs/alpaca_data_500"
)

log_step(f"Selected {len(selected)} records")

save_result(selected)'''

# DSL Specification
DSL_SPECIFICATION = """
## Pipeline DSL Functions

The following functions are available in Pipeline DSL:

### load_dataset(dataset_name)
Load dataset from configured database.
Returns: list[dict]

### run_tool(tool_name, **kwargs)
Execute a registered tool.
Returns: Any (depends on tool implementation)

### save_result(data)
Save result to job output (accessible via `pilot result`).

### write_file(data, path)
Write data to a local JSON file.
Example: write_file(data, "./output/result.json")

### write_db(data, table)
Write data to configured database table.
Example: write_db(data, "processed_records")

### log_step(message)
Log pipeline execution progress.

## Data Format
All pipeline data must be list[dict] format.
Example:
[{"id": 1, "name": "example", "value": 100}]
"""

# Tool Usage Rules
TOOL_USAGE_RULES = """
## Tool Usage Rules

Tools must always be called using run_tool().

Example:
result = run_tool(
    "tool_name",
    data=data
)

Important Rules:
1. Tools cannot access database directly.
2. Tools must receive data from previous steps.
3. Tool parameters must match schema definitions.
4. Always use log_step() immediately before and immediately after every DSL function call.
5. At minimum, add before/after log_step() calls around load_dataset(), run_tool(), save_result(), write_file(), and write_db().
"""

# Pipeline Generation Rules
PIPELINE_GENERATION_RULES = """
## Pipeline Generation Rules

Rules:
1. If a tool needs data from a database/dataset, use load_dataset() first. Some tools (e.g. enzyme_acquire, protein_analyzer) accept data directly as parameters and do NOT require load_dataset().
2. Each tool must use data generated from previous steps, or data defined inline in the pipeline.
3. Always use log_step() immediately before and immediately after every DSL function call.
4. Always save final results using save_result().
5. Do not import Python libraries.
6. Do not write arbitrary Python code.
7. Only use DSL functions defined above.
8. The pipeline must be a valid Python code that can be executed by exec().
9. Tool results can be chained (e.g., scoring output → selection input).
10. Always pass dataset_name to data_select matching the name used in load_dataset(),
   so embeddings are cached and reused correctly across runs.
11. Use RELATIVE paths for output_dir, e.g. "outputs/{dataset}_{budget}".
   Derive names from the dataset name and parameters used in the pipeline.
12. Every load_dataset(), run_tool(), save_result(), write_file(), and write_db() call must have a log_step() before it and another log_step() after it.
13. When the user asks for data selection without specifying a scoring method,
   always score with 'dataelf' first, then select. 'dataelf' is the default
   and recommended scorer.
14. Do not infer a tool intent from the dataset name alone. For example,
   a dataset named security_audit_samples does NOT mean you should call
   security_audit unless the user explicitly asks for an audit, checkers,
   risk/compliance analysis, or similar security evaluation.
15. For run_tool(), pass only parameters that are explicitly present in that tool's schema.
   Do not invent generic arguments like data=... for derived or experimental tools unless
   the schema actually declares a data parameter.
16. When Planner note, Revision instructions, or Repair context mention a previous error,
   treat that error as a concrete repair target for the next pipeline.
17. You may use derived tools or experimental tools when the user task or planner guidance
   explicitly prefers them, but neither path is mandatory by default.
18. If you call a derived or experimental tool, inspect its schema carefully and only pass
   supported parameters.
19. Only call tools that already exist in the injected Available Tools list / tool schemas.
   Do not call a speculative future tool name in the same attempt before it has been derived,
   validated, and registered.
20. After repeated derived/experimental tool failures such as tool_not_found,
   tool_parameter_error, or tool_runtime_error, consider solving the task directly with
   built-in DSL primitives instead of retrying the same failing tool path.
21. When planner guidance or Available Tools indicate that an existing tool is semantically relevant
   but operationally slow, you may route the attempt toward evidence collection for a derived
   experimental optimization draft.
22. If a derived/experimental tool is already available for this attempt, you may use it; otherwise
   do not invent a same-attempt future tool call.
"""

# Output Format
OUTPUT_FORMAT = """
## Output Format

Output only valid Pipeline DSL code.
Do not explain.
Do not add comments outside code.
Do not include markdown.
Do not wrap code in triple backticks.
"""

# Agent Role
AGENT_ROLE = """
You are an AI data pipeline planner.

Your task is to convert a user request into a valid pipeline written in the DataElf Pipeline DSL.

The generated pipeline will be executed by a runtime engine.

You must only generate valid DSL code.
"""


README_SECTION_PRIORITY = [
    "overview",
    "input schema",
    "parameters",
    "output",
    "example",
    "checkers",
    "algorithms",
    "modules",
    "features",
    "configuration",
    "dependencies",
]


def resolve_tool_readme_path(tool_name: str, docs_dir: Path | None = None) -> Path | None:
    docs_root = docs_dir or (Path(__file__).parent.parent / "docs" / "tools")
    candidates = [
        docs_root / f"{tool_name}_zh.md",
        docs_root / f"{tool_name}.md",
        docs_root / f"{tool_name}_en.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def truncate_tool_readme_by_priority(content: str, max_len: int) -> str:
    if len(content) <= max_len:
        return content.strip()

    section_pattern = re.compile(r"^##\s+(.+)$", re.MULTILINE)
    splits = list(section_pattern.finditer(content))
    if not splits:
        return content[:max_len].strip()

    sections: list[tuple[str, str]] = []
    for i, match in enumerate(splits):
        title = match.group(1).strip()
        start = match.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(content)
        body = content[start:end].strip()
        sections.append((title, body))

    priority_map: dict[int, int] = {}
    for idx, (title, _) in enumerate(sections):
        title_lower = title.lower()
        for pri, keyword in enumerate(README_SECTION_PRIORITY):
            if title_lower.startswith(keyword):
                priority_map[idx] = pri
                break

    sorted_indices = sorted(
        range(len(sections)),
        key=lambda i: priority_map.get(i, len(README_SECTION_PRIORITY)),
    )

    result_parts: list[str] = []
    current_len = 0
    for idx in sorted_indices:
        title, body = sections[idx]
        section_text = f"## {title}\n\n{body}"
        if current_len + len(section_text) <= max_len:
            result_parts.append(section_text)
            current_len += len(section_text)

    return "\n\n".join(result_parts).strip() if result_parts else content[:max_len].strip()


def load_tool_readme_entries(
    tool_names: list[str],
    max_len: int = 2000,
    docs_dir: Path | None = None,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for tool_name in tool_names:
        readme_path = resolve_tool_readme_path(tool_name, docs_dir=docs_dir)
        if readme_path is None:
            continue
        content = readme_path.read_text(encoding="utf-8")
        entries.append({
            "tool_name": tool_name,
            "path": str(readme_path),
            "content": truncate_tool_readme_by_priority(content, max_len),
        })
    return entries


class PromptBuilder:
    """
    The prompt structure follows this pattern:
    1. System Role
    2. Pipeline DSL Specification
    3. Tool Usage Rules
    4. Available Tools (dynamically injected from ToolRegistry)
    5. Pipeline Generation Rules
    6. Pipeline Example
    7. User Request
    8. Output Format
    """

    def __init__(
        self,
        dsl_spec: str | None = None,
        tool_spec: str | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        pipeline_example: str | None = None,
        dataset_schemas: dict[str, list[str]] | None = None,
        tool_names: list[str] | None = None,
        tool_readmes_max_length: int = 2000,
    ):

        self.dsl_spec = dsl_spec or DSL_SPECIFICATION
        self.tool_spec = tool_spec or ""
        self.tool_schemas = tool_schemas or []
        self.pipeline_example = pipeline_example or DEFAULT_PIPELINE_EXAMPLE
        self.dataset_schemas = dataset_schemas or {}
        self.tool_names = tool_names or []
        self.tool_readmes_max_length = tool_readmes_max_length

    def set_tool_schemas(self, schemas: list[dict[str, Any]]) -> None:
        #Set tool schemas from ToolRegistry
        self.tool_schemas = schemas

    def build_messages(self, user_query: str) -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for the LLM call.

        Splitting system/user roles improves instruction adherence — the model
        treats system content as hard constraints and user content as the task.
        """
        # Build system sections (everything except user request)
        sections = [
            self._build_role_section(),
            self._build_dsl_section(),
            self._build_available_datasets_section(),
            self._build_tool_usage_section(),
            self._build_available_tools_section(),
        ]

        # Add tool READMEs if tool_names are provided
        readme_section = self._build_tool_readmes_section()
        if readme_section:
            sections.append(readme_section)

        sections += [
            self._build_generation_rules_section(),
            self._build_example_section(),
            self._build_output_format_section(),
        ]

        system_prompt = "\n\n".join(sections)
        user_prompt = f"## User Request\n\n{user_query}"
        return system_prompt, user_prompt

    #Build the complete prompt for pipeline generation.
    def build_agent_prompt(self, user_query: str) -> str:
        system_prompt, user_prompt = self.build_messages(user_query)
        return system_prompt + "\n\n" + user_prompt

    def _build_role_section(self) -> str:
        return AGENT_ROLE.strip()

    def _build_dsl_section(self) -> str:
        return f"## Pipeline DSL Specification\n\n{self.dsl_spec.strip()}"

    def _build_available_datasets_section(self) -> str:
        if not self.dataset_schemas:
            return "## Available Datasets\n\nNo dataset schema information available."

        lines = ["## Available Datasets\n"]
        lines.append("The following datasets are available via load_dataset(). Use the exact field names when filtering.\n")

        for table_name, fields in sorted(self.dataset_schemas.items()):
            lines.append(f"### `{table_name}`")
            lines.append(f"Fields: {', '.join(f'`{f}`' for f in fields)}\n")

        lines.append("Example: load_dataset(\"table_name\", filters={\"field_name\": \"value\"})")
        return "\n".join(lines)

    def _build_tool_usage_section(self) -> str:
        sections = [TOOL_USAGE_RULES.strip()]
        if self.tool_spec:
            sections.append(f"## Tool Specification\n\n{self.tool_spec.strip()}")
        return "\n\n".join(sections)

    def _build_available_tools_section(self) -> str:
        if not self.tool_schemas:
            return "## Available Tools\n\nNo tools available."

        lines = ["## Available Tools"]

        for schema in self.tool_schemas:
            name = schema.get("name", "unknown")
            description = schema.get("description", "")
            parameters = schema.get("parameters", {})
            usage_example = schema.get("usage_example", "")

            lines.append(f"\n### Tool: {name}")
            lines.append(f"\nDescription:\n{description}")
            lines.append(f"\nParameters:")
            lines.append(self._format_parameters(parameters))

            if usage_example:
                lines.append(f"\nExample usage:")
                lines.append(f"```\n{usage_example}\n```")

            lines.append("")

        return "\n".join(lines)

    def _format_parameters(self, parameters: dict[str, Any]) -> str:
        if not parameters:
            return "No parameters"

        props = parameters.get("properties", {})
        required = parameters.get("required", [])

        lines = []
        for param_name, param_info in props.items():
            param_type = param_info.get("type", "any")
            description = param_info.get("description", "")
            default = param_info.get("default")

            required_indicator = " (required)" if param_name in required else " (optional)"
            default_str = f", default={default}" if default is not None else ""

            lines.append(f"- {param_name}: {param_type}{required_indicator}{default_str}")
            if description:
                lines.append(f"  - {description}")

        return "\n".join(lines) if lines else "No parameters"

    def _build_generation_rules_section(self) -> str:
        return f"## Pipeline Generation Rules\n\n{PIPELINE_GENERATION_RULES.strip()}"

    def _build_example_section(self) -> str:
        return f"""## Pipeline Example

```
{self.pipeline_example}
```"""

    def _build_user_request_section(self, user_query: str) -> str:
        return f"## User Request\n\n{user_query}"

    def _build_output_format_section(self) -> str:
        return f"## Output Format\n\n{OUTPUT_FORMAT.strip()}"

    def _build_tool_readmes_section(self) -> str:
        """Load and truncate tool READMEs from docs/tools/ by priority."""
        if not self.tool_names:
            return ""

        entries = [
            f"### {entry['tool_name']}\n\n{entry['content']}"
            for entry in load_tool_readme_entries(self.tool_names, self.tool_readmes_max_length)
        ]

        if not entries:
            return ""

        return "## Tool Documentation\n\n" + "\n\n".join(entries)

#tool_schemas: List of tool schemas from ToolRegistry.
#dsl_spec_path: Path to DSL specification markdown file.
#tool_spec_path: Path to tool specification markdown file.
#tool_names: Tool names to load READMEs for (from config.tools).
#tool_readmes_max_length: Max characters per README.
def create_prompt_builder(
    tool_schemas: list[dict[str, Any]] | None = None,
    dsl_spec_path: str | None = None,
    tool_spec_path: str | None = None,
    dataset_schemas: dict[str, list[str]] | None = None,
    tool_names: list[str] | None = None,
    tool_readmes_max_length: int = 2000,
) -> PromptBuilder:
    # Load DSL spec from file if path provided
    dsl_spec = ""
    if dsl_spec_path:
        spec_path = Path(dsl_spec_path)
        if spec_path.exists():
            dsl_spec = spec_path.read_text()

    # Load tool spec from file if path provided
    tool_spec = ""
    if tool_spec_path:
        spec_path = Path(tool_spec_path)
        if spec_path.exists():
            tool_spec = spec_path.read_text()

    # If no file paths, use defaults
    if not dsl_spec:
        dsl_spec = DSL_SPECIFICATION

    return PromptBuilder(
        dsl_spec=dsl_spec,
        tool_spec=tool_spec,
        tool_schemas=tool_schemas or [],
        dataset_schemas=dataset_schemas or {},
        tool_names=tool_names or [],
        tool_readmes_max_length=tool_readmes_max_length,
    )


def build_agent_prompt(
    user_query: str,
    tool_schemas: list[dict[str, Any]],
    dsl_spec_path: str | None = None,
    tool_spec_path: str | None = None,
) -> str:
    
    builder = create_prompt_builder(
        tool_schemas=tool_schemas,
        dsl_spec_path=dsl_spec_path,
        tool_spec_path=tool_spec_path,
    )
    return builder.build_agent_prompt(user_query)
