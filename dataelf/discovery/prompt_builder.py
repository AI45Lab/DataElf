from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob, OutputContract
from dataelf.discovery.execution_policy import ai_index_research, unbounded_analysis


def write_discovery_prompt(
    job: DiscoveryJob,
    context: DiscoveryContext,
    domain_instructions: str,
    output_contract: OutputContract,
) -> Path:
    if not ai_index_research(job.spec):
        from .flows.legacy_prompt import write_discovery_prompt as legacy_prompt
        return legacy_prompt(job, context, domain_instructions, output_contract)
    workspace = Path(context.workspace_path).resolve()
    prompt_path = workspace / "prompts" / "discovery_prompt.md"
    prompt_path.parent.mkdir(parents=True,exist_ok=True)
    if ai_index_research(job.spec):
        from .output_submission import submission_schema
        (prompt_path.parent / 'output_contract.json').write_text(json.dumps({
            'contract':output_contract.model_dump(mode='json'),
            'protected_paths':[a.path for a in context.artifacts if a.role in {'input','evidence'}]},
            ensure_ascii=False,indent=2),encoding='utf-8')
        schema = submission_schema(output_contract)
        if schema is not None:
            from dataelf.ontology.wire_schema import provider_schema
            (prompt_path.parent / 'output_tool_schema.json').write_text(json.dumps(provider_schema(schema)),encoding='utf-8')
    prompt_path.write_text(
        compose_discovery_prompt(job, context, domain_instructions, output_contract),
        encoding="utf-8",
    )
    return prompt_path


def compose_discovery_prompt(
    job: DiscoveryJob,
    context: DiscoveryContext,
    domain_instructions: str,
    output_contract: OutputContract,
) -> str:
    if not ai_index_research(job.spec):
        from .flows.legacy_prompt import compose_discovery_prompt as legacy_prompt
        return legacy_prompt(job, context, domain_instructions, output_contract)
    from dataelf.ontology.capabilities import skill_review
    if skill_review(job.spec):
        # The selected plugin owns its composed task; keep Discovery neutral.
        return domain_instructions
    workspace = Path(context.workspace_path).resolve()
    spec_json = json.dumps(job.spec.model_dump(mode="json"), ensure_ascii=False, indent=2)
    artifact_json = json.dumps([item.model_dump(mode="json") for item in context.artifacts], ensure_ascii=False, indent=2)
    outputs = "\n".join(
        f"- `{item.path}` ({'required' if item.required else 'optional'}, {item.kind})"
        + (f' JSON ROOT: {{"{item.json_root}": [...]}}; never a top-level array.' if item.json_root and ai_index_research(job.spec) else '')
        for item in output_contract.artifacts
    )
    model_line = f"\nPreferred model: `{context.model}`\n" if context.model else ""
    graph_instructions = (
        f"Graph reference for this analysis: {json.dumps(context.graph_ref, ensure_ascii=False)}. "
        "Record the revision(s) used in your report. Shared graph tools support query, "
        "exploration and evidence-backed candidate maintenance.\n"
        if context.graph_ref else ""
    )
    submission_instructions = ('''For declared agent-writable JSON artifacts, use dataelf_submit_output with direct object arguments
{"artifact_id":"<declared ID>","data":{...complete output...}}. The runtime validates and writes the file.
Never JSON-serialize data into a string. Raw source evidence is immutable and is not submitted through this tool.
''' if ai_index_research(job.spec) else '')
    completion = ("Save the complete outputs and use analysis_submit for independent acceptance. Correct validation errors or reviewer issues and resubmit. File existence and ordinary prose do not complete the task."
                  if ai_index_research(job.spec) else
                  "Before stopping, verify every required output exists, is non-empty, and stays inside the workspace. JSON outputs must be valid JSON. Once the outputs are verified, stop.")
    scope = 'one DataElf job' if unbounded_analysis(job.spec) else 'one bounded DataElf job'
    return f"""# DataElf Task

You are the Pi explorer running {scope}. The core runtime owns lifecycle, workspace isolation, artifact validation, and review. You own analysis and creation of the declared domain outputs.
{model_line}
## Workspace

Write every generated file under this workspace and nowhere else:

`{workspace}`

The process working directory may be the DataElf repository for Pi configuration. That does not change the artifact boundary above. Do not modify DataElf source code.

## Job specification

```json
{spec_json}
```

## Prepared and modeled artifacts

```json
{artifact_json}
```
{graph_instructions}
## Required outputs

Output contract `{output_contract.contract_id}` version `{output_contract.version}`:

{outputs}

## Domain instructions

{domain_instructions.strip()}

## Completion rule

{completion}
{submission_instructions}
"""


__all__ = ["compose_discovery_prompt", "write_discovery_prompt"]
