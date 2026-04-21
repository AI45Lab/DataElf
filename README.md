<p align="center">
  <img src="./DataElf_logo.jpeg" alt="DataElf logo" width="320" />
</p>

# DataElf

DataElf is an intelligent execution engine for large-scale data workflows. It turns natural-language goals into runnable pipelines, executes built-in tools automatically, and keeps security, traceability, and extensibility in the loop.

The open-source edition is designed for teams that want one framework for data inspection, safety checks, scoring, selection, and domain-oriented tool orchestration without exposing private data-processing infrastructure.

## What DataElf Does

- Translate natural language into executable pipelines.
- Run a single-pass workflow for clear, bounded tasks with lightweight clarification when needed.
- Run a higher-autonomy pilot loop for harder tasks that require planning, execution, judging, repair, and asset derivation.
- Preserve execution artifacts such as pipelines, logs, reports, and intermediate outputs for review.
- Support reusable stable workflow assets through approval and submission.
- Let teams extend the tool layer with custom `BaseTool` implementations.
- Combine data-safety checks with broader data-processing workflows in one CLI-driven system.

## Built-In Tools

DataElf currently ships with these built-in tools:

| Tool | Focus | Docs |
| --- | --- | --- |
| `security_audit` | Safety and risk scanning for datasets | [security_audit_en.md](docs/tools/security_audit_en.md) |
| `data_scoring` | Sample-level quality scoring | [data_scoring_en.md](docs/tools/data_scoring_en.md) |
| `data_select` | Budget-aware or cluster-based data selection | [data_select_en.md](docs/tools/data_select_en.md) |
| `enzyme_acquire` | Enzyme information retrieval workflows | [enzyme_acquire_en.md](docs/tools/enzyme_acquire_en.md) |
| `protein_analyzer` | Protein analysis workflows | [protein_analyzer_en.md](docs/tools/protein_analyzer_en.md) |
| `skillrl_skill_extraction` | Skill extraction from trajectory-style data | [skillrl_skill_extraction_en.md](docs/tools/skillrl_skill_extraction_en.md) |

## Installation

Clone the repository and install the package:

```bash
git clone https://github.com/<your-org>/DataElf.git
cd DataElf
pip install -e .
```

If you want all currently built-in tools available in one environment, install the optional dependency groups as well:

```bash
pip install -e ".[scitools,scoring,security_audit]"
```

After installation, the CLI is available as:

```bash
elf --help
```

## Configuration

The repository now keeps a single public example config at [`config.yaml`](config.yaml). It is written for open-source usage:

- LLM credentials are read from environment variables.
- The open-source build supports `local_file` and `mock` database strategies.
- The example config lists all built-in tools.

Set environment variables before running:

```bash
export OPENAI_API_KEY="<your-api-key>"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export DATAELF_AGENT_MODEL="gpt-4o-mini"
export DATAELF_TOOL_LLM_MODEL="gpt-4o-mini"
```

The default config uses the local JSON datasets in `./test_data`, so you can point DataElf at repository fixtures or replace that path with your own dataset directory.

## Quick Start

Run a single task:

```bash
elf run "run security_audit on security_audit_samples with a custom checker set" -c config.yaml --wait
```

Run a scoring task:

```bash
elf run "score the alpaca_data dataset and summarize the highest-quality records" -c config.yaml --wait
```

Run the higher-autonomy pilot loop:

```bash
elf pilot "screen alpaca_data for high-value and low-risk samples" -c config.yaml --wait --budget-steps 3
```

Check outputs later:

```bash
elf status <job_id>
elf result <job_id> --json --artifacts
elf inspect <job_id|candidate_id|asset_id> --json
```

Approve and reuse a promoted workflow asset:

```bash
elf approve <candidate_id>
elf submit <asset_id> -c config.yaml --wait
```

## CLI Overview

```bash
elf run "task" [--config PATH] [--wait] [--verbose]
elf pilot "task" [--config PATH] [--wait] [--budget-steps N] [--allow-experimental-tools]
elf status <job_id>
elf result <job_id> [--json] [--artifacts]
elf approve <candidate_id>
elf submit <asset_id> [--config PATH] [--wait]
elf inspect <job_id|candidate_id|asset_id> [--json]
```

## Execution Modes

- `run`: single-pass planning and execution for relatively clear tasks.
- `pilot`: iterative planning and repair loop with candidate asset generation.
- `submit`: execute an approved stable pipeline asset.

## Extending DataElf

You can add your own tools by implementing `BaseTool`, wiring the tool into the registry, and listing it in `config.yaml`.

Minimal example:

```python
from typing import Any

from tools import BaseTool, ToolContext


class DomainRiskCheckTool(BaseTool):
    @property
    def name(self) -> str:
        return "domain_risk_check"

    @property
    def description(self) -> str:
        return "Check domain-specific risky patterns in dataset records."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "mode": {
                    "type": "string",
                    "default": "strict",
                },
            },
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data = kwargs.get("data", [])
        mode = kwargs.get("mode", "strict")
        checked = data
        return {
            "result": checked,
            "metadata": {
                "records_processed": len(data),
                "mode": mode,
            },
            "artifacts": {
                "report_md": "# Domain Risk Report\n",
            },
        }
```

See the developer docs for details:

- [Tool development guide](docs/tool_development_guide.md)
- [Tool spec](docs/tool_spec.md)
- [Pipeline DSL spec](docs/pipeline_dsl_spec.md)

## Project Structure

```text
DataElf/
├── ai_data_pilot/   # CLI entry package
├── cli/             # Command implementations
├── agent/           # Agent adapters and prompt building
├── agentic/         # Pilot loop and asset lifecycle
├── runtime/         # Runtime execution layer
├── tools/           # Built-in tools
├── database/        # Open-source database strategies
├── config/          # Config loading
├── docs/            # User and developer documentation
├── test_data/       # Local sample datasets
└── config.yaml      # Public example config
```
