from __future__ import annotations

from dataelf.schemas import ToolSpec


TOOL_SPECS = [
    ToolSpec(name="search_papers", description="Search AI Index papers for discovery.", input_schema={}, output_schema={}, permission="read"),
    ToolSpec(name="search_institutions", description="Search AI Index institutions for discovery.", input_schema={}, output_schema={}, permission="read"),
    ToolSpec(name="search_scholars", description="Search AI Index scholars for discovery.", input_schema={}, output_schema={}, permission="read"),
    ToolSpec(name="fetch_institution_funding", description="Fetch an institution funding profile from AI Index.", input_schema={}, output_schema={}, permission="read"),
]


def list_tool_specs() -> list[ToolSpec]:
    return TOOL_SPECS
