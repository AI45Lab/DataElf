from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob, OutputContract


def write_discovery_prompt(
    job: DiscoveryJob,
    context: DiscoveryContext,
    domain_instructions: str,
    output_contract: OutputContract,
) -> Path:
    workspace = Path(context.workspace_path).resolve()
    prompt_path = workspace / "prompts" / "discovery_prompt.md"
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
    workspace = Path(context.workspace_path).resolve()
    spec_json = json.dumps(job.spec.model_dump(mode="json"), ensure_ascii=False, indent=2)
    artifact_json = json.dumps([item.model_dump(mode="json") for item in context.artifacts], ensure_ascii=False, indent=2)
    outputs = "\n".join(
        f"- `{item.path}` ({'required' if item.required else 'optional'}, {item.kind})"
        for item in output_contract.artifacts
    )
    model_line = f"\nPreferred model: `{context.model}`\n" if context.model else ""
    return f"""# DataElf Task

You are the Pi explorer running one bounded DataElf job. The core runtime owns lifecycle, workspace isolation, artifact validation, and review. You own analysis and creation of the declared domain outputs.
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

## Required outputs

Output contract `{output_contract.contract_id}` version `{output_contract.version}`:

{outputs}

## Domain instructions

{domain_instructions.strip()}

## Completion rule

Before stopping, verify every required output exists, is non-empty, and stays inside the workspace. JSON outputs must be valid JSON. Once the outputs are verified, stop.
"""


__all__ = ["compose_discovery_prompt", "write_discovery_prompt"]
