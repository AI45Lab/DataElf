# DataElf

DataElf is a multi-domain analysis runtime. Its core owns job lifecycle, isolated workspaces, Pi execution, artifact validation, and finalization. Each domain owns data preparation, optional modeling, analysis instructions, output contracts, and semantic review.

```text
CLI / API
  -> JobSpec
  -> typed DomainPlugin
  -> domain adapter
  -> optional domain modeler
  -> composed Pi prompt
  -> generic output validation
  -> domain review
```

The current built-in domain is `ai_index`. All cases use the same domain-aware entrypoint:

```bash
dataelf run --domain ai_index "围绕 Agentic LLMs，基于 AI Index，发现最近值得关注的 3 个 insight"
```

Internally this creates `JobSpec(domain="ai_index", objective=...)`; the CLI does not infer a domain from arbitrary natural language.

## Install

Requirements: Python 3.11+, Node.js 22.19+, and npm.

```bash
uv venv
uv pip install -e ".[dev]"
dataelf setup
```

`dataelf setup` prepares the project-local explorer runtime, including the locked Node/Pi dependency and the Pi analysis package. It uses a cache inside the project, so users do not need to run npm or Pi package commands themselves. Run it once after installing the Python package, and run it again if the runtime is removed or the lockfile changes.

Create a local configuration file:

```bash
dataelf init
```

`dataelf.local.yaml` is ignored by git and should contain local credentials.

## Quick start

After cloning the repository, run the following from the project root:

```bash
uv venv
uv pip install -e ".[dev]"
dataelf setup
dataelf init
```

Then edit the generated `dataelf.local.yaml` using the nested configuration shown below. At minimum, provide the selected Pi model and the required local API credentials under `env`; for the built-in AI Index domain, configure `domains.ai_index.source` as `api` or `fixture`.

Run the built-in domain through the same domain-aware entrypoint used by every future case:

```bash
dataelf run --domain ai_index \
  "围绕 Agentic LLMs，基于 AI Index 和联网搜索，发现最近值得关注的 1 个 insight"
```

On Windows PowerShell, activate the virtual environment with `.venv\\Scripts\\Activate.ps1` and use `.venv\\Scripts\\dataelf.exe`; `dataelf setup` handles the platform-specific runtime details.

## Configuration

DataElf has one nested configuration schema. There is no flat legacy schema and no alternate explorer configuration path.

```yaml
runtime:
  workspace_dir: .dataelf
  enable_sqlite: false

explorer:
  type: pi
  pi:
    # Optional; dataelf setup selects the project-local runtime by default.
    binary:
    model:
    mode: json
    cwd: .
    timeout_seconds:
    extra_args: ""
    log_mode: summary

domains:
  ai_index:
    source:
      mode: api
      base_url: https://index.shlab.org.cn/api/v2
      api_key: ak_...
      fixtures_dir: fixtures/ai_index
    modeling:
      enabled: false
      # ai_index_search uses the reviewed fixed Stage 1 template.
      ontology_template:
      stage1_config: dataelf/domains/ai_index/modeling/ontology/stage1/config.yaml
      stage2_config: dataelf/domains/ai_index/modeling/ontology/stage2/config.yaml
      raw_page_size: 50
      model_name:
      model_max_tokens:
      stage1_process_timeout_seconds: 7200
      stage1_request_timeout_seconds: 900
      stage1_request_max_retries: 3
      stage2_request_timeout_seconds: 600
      stage2_request_max_retries: 3
      stage2_total_timeout_seconds: 1800

env:
  PI_CODING_AGENT_DIR: .pi/agent
  OPENAI_BASE_URL: https://example.com/v1
  OPENAI_API_KEY: sk-...
  # BRAVE_API_KEY: ...  # only when a Brave search skill is loaded
```

Configuration order:

1. Built-in defaults.
2. The first existing supported YAML/JSON config file.
3. Environment variables.

Useful overrides include `DATAELF_WORKSPACE`, `DATAELF_PI_BINARY`, `DATAELF_PI_MODEL`, `DATAELF_PI_LOG_MODE`, `DATAELF_AI_INDEX_MODE`, `DATAELF_FIXTURES_DIR`, `AI_INDEX_BASE_URL`, `AI_INDEX_API_KEY`, and `DATAELF_AI_INDEX_MODELING_ENABLED`.

The Pi process receives only an allowlisted process environment plus the explicit `env` mapping and environment prepared by the selected domain adapter.

## Multi-domain contract

Domain discovery is manifest-driven. A domain lives under `dataelf/domains/<domain>/` and provides:

```text
domain.yaml        typed identity, plugin entrypoint, capabilities, workspace directories
plugin.py          DomainPlugin implementation
config.py          domain-owned typed configuration
prompt.py          domain analysis method and evidence instructions
review.py          domain semantic review
```

`DomainPlugin` implements these stages:

- `normalize_spec`: adds deterministic domain parameters without changing the user's objective.
- `prepare`: creates domain directories and prepares data access, context, environment, and input artifacts.
- `create_modeler`: optionally returns a domain modeler.
- `build_prompt`: supplies only domain instructions; the core composes runtime, workspace, job, artifact, and output-contract sections.
- `output_contract`: declares required outputs.
- `review`: applies semantic checks after generic artifact validation.
- `result_ids`: exposes the domain's primary result identifiers for the workspace index.

Every stage communicates through `ArtifactRef` and `StageResult`. Core output validation checks workspace containment, required/non-empty files, JSON validity, and declared JSON roots. The final `artifact_manifest.json` is therefore domain-neutral.

Adding another domain does not require edits to workflow, workspace creation, prompt composition, Pi execution, or generic output validation. Tests include a fake domain with its own adapter, optional modeler, output contract, and reviewer to enforce this boundary.

## AI Index domain

The AI Index plugin owns:

- `raw/ai_index/` and `raw/web/` workspace directories;
- normalized table schemas;
- source credentials and fixture/API selection;
- dynamic `AIIndexClient` access;
- optional ontology/RDF modeling;
- the four-phase technology-intelligence prompt;
- insight output contracts and quality review.

Scripts written by Pi can fetch additional data through:

```python
from dataelf.domains.ai_index.client import AIIndexClient

client = AIIndexClient.from_env()
papers = client.search_papers(sub_domains=["Agentic LLMs"], page=1, size=50)
```

The connector supports paper, institution, and scholar search plus institution funding profiles. API responses are persisted under the job workspace and normalized into CSV tables.

Enable ontology/RDF modeling for one CLI run:

```bash
dataelf run --domain ai_index --modeling \
  "围绕 Agentic LLMs，发现最近值得关注的 3 个 insight"
```

Use the fixed reviewed template:

```bash
dataelf run --domain ai_index --modeling --ontology-template ai_index_search \
  "围绕 Agentic LLMs，发现最近值得关注的 3 个 insight"
```

The modeler returns standard evidence artifacts; it does not replace the core prompt path. Detailed ontology operation and troubleshooting are documented in [`dataelf/domains/ai_index/modeling/ontology/README.md`](dataelf/domains/ai_index/modeling/ontology/README.md), with module responsibilities in [`ARCHITECTURE.md`](dataelf/domains/ai_index/modeling/ontology/ARCHITECTURE.md).

## Workspace

Core creates only the generic skeleton; the domain adds its own directories:

```text
.dataelf/workspaces/<job_id>/
  job_spec.json
  raw/
  tables/
  scripts/
  notes/
  prompts/discovery_prompt.md
  logs/
  reviews/quality_review.json
  artifacts/
  artifact_manifest.json
  workspace_index.json
```

AI Index additionally creates `raw/ai_index/`, `raw/web/`, `deep_dives/`, and `insights/`. Final output files are not pre-created; their presence means the explorer actually produced them.

Set `runtime.enable_sqlite: true` only when job lookup commands are required. The workspace remains the source of job artifacts regardless of registry mode.

## Pi

Project Pi settings live in `.pi/settings.json`, `.pi/agent/models.json`, and `pi-harness.config.json`. `pi-harness.config.json` belongs to `@quarkos/pi-fusion` and is resolved from the configured Pi working directory.

The supported user-facing setup path is `dataelf setup`. It owns the project-local Pi/npm lifecycle and verifies the runtime before a job starts; the files above are implementation/configuration details for maintainers and advanced integrations.

DataElf selects the Explorer model through `explorer.pi.model` (for example, `boyuerich-openai/deepseek-v4-pro`). Pi owns the provider registry, endpoint, protocol, and model metadata in `.pi/agent/models.json`; credentials should be supplied through the local `env` mapping (for example, `OPENAI_API_KEY`) and referenced by the provider configuration. `.pi/settings.json` supplies Pi's fallback provider/model only when DataElf does not pass an explicit model.

`explorer.pi.log_mode` can be `quiet`, `summary`, or `raw`. Raw JSON events are always saved to `logs/pi_events.jsonl`; terminal verbosity does not change the artifact contract.

Official Pi CLI resource flags belong in `explorer.pi.extra_args`, for example:

```yaml
explorer:
  type: pi
  pi:
    extra_args: "--skill /path/to/pi-skills/brave-search"
```

## Verify

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q dataelf
```
