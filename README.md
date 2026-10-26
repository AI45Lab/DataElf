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

The source tree includes `ai_index`, `trajectory_analysis`, and `finance`. All cases use the same domain-aware entrypoint:

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

Use [dataelf.local.yaml.example](dataelf.local.yaml.example) as the main configuration
reference, preserving any existing local file. Fill the empty model, source URL,
credential and data-path fields for the domain you use before running. Configure
your own provider/model in your existing local Pi registry; personal Pi files are
not part of this change. Prefer process environment credentials. Empty service
intent URL/key values read `OPENAI_BASE_URL` and `OPENAI_API_KEY`; its model ID must
be filled explicitly. Ontology role models inherit `explorer.pi.model` when empty.
The examples parse as YAML/JSON but require these deployment values before use.

Select `--workflow-policy ai_index_cli_skill_review_v1` explicitly for the reviewed
AI Index research workflow. Other domains retain `legacy`; HTTP serve uses its
own fixed Scope V2 templates and service budgets regardless of root ontology.

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
    model: ""
    mode: json
    cwd: .
    timeout_seconds:
    extra_args: ""
    log_mode: summary

domains:
  ai_index:
    source:
      mode: api
      base_url: ""
      api_key: ""
      fixtures_dir: fixtures/ai_index

ontology:
  enabled: false
  ontology_config: dataelf/ontology/config.yaml

env:
  PI_CODING_AGENT_DIR: .pi/agent
  OPENAI_BASE_URL: ""
  OPENAI_API_KEY: ""
  # BRAVE_API_KEY: ""  # only when a Brave search skill is loaded
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

### Domain Pi resources

Pi's built-in tools (`read`, `bash`, `edit`, `write`, `grep`, `find`, and `ls`) are provided by Pi as the common agent base. DataElf does not define a second tool registry and does not convert Python helpers into Pi tools.

A domain may add Pi-native behavior through the official Pi extension mechanism. An extension is a JavaScript/TypeScript module and may register one or more model-callable tools with `pi.registerTool(...)`. Keep those files under the domain:

```text
dataelf/domains/<domain>/
├── tools.py                 # optional Python helpers for analysis scripts
└── pi/
    ├── extensions/          # optional Pi .js/.mjs/.ts extensions
    └── skills/              # optional directories containing SKILL.md
```

At run time DataElf discovers the domain's `pi/extensions/` files and `pi/skills/**/SKILL.md`, then passes them to Pi with explicit `--extension` and `--skill` flags. Python `tools.py` remains an internal library used by scripts; it is not automatically visible to Pi. The AI Index domain currently uses this Python pattern through `AIIndexClient`.

Every stage communicates through `ArtifactRef` and `StageResult`. Core output validation checks workspace containment, required/non-empty files, JSON validity, and declared JSON roots. The final `artifact_manifest.json` is therefore domain-neutral.

Adding another domain does not require edits to workflow, workspace creation, prompt composition, Pi execution, or generic output validation. Tests include a fake domain with its own adapter, optional modeler, output contract, and reviewer to enforce this boundary.

## AI Index domain

The AI Index plugin owns:

- `raw/ai_index/` and `raw/web/` workspace directories;
- normalized table schemas;
- source credentials and fixture/API selection;
- dynamic `AIIndexClient` access;
- optional ontology/RDF modeling;
- autonomous evidence-driven research with an independent reviewer;
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
dataelf run --domain ai_index --workflow-policy ai_index_cli_skill_review_v1 --modeling \
  "围绕 Agentic LLMs，发现最近值得关注的 3 个 insight"
```

Ontology now lives in `dataelf/ontology`. The main config contains only
`ontology.enabled` and `ontology.ontology_config`. Copy the packaged
`dataelf/ontology/config.yaml.example` to `config.yaml` to customize the
modeler, reviewer, repair budget and output directory.

Dynamic mode uses one autonomous modeler and one independent reviewer through
a shared versioned GraphService. Modeling and analysis use the same graph tools;
the deterministic engine executes the reviewed contract. AI Index CLI uses dynamic
contracts; its former template-selection/export entry is retired. To continue
analysis from an approved revision without another modeling call:

```bash
dataelf run --domain ai_index --workflow-policy ai_index_cli_skill_review_v1 --resume-graph-manifest /absolute/revisions/r-.../manifest.json \
  "继续核查这个已发布版本的事实与来源"
```

AI Index research, with or without ontology, submits complete report snapshots to
an independent analyst/reviewer loop. It has no cumulative time, request, token or
review-round deadline; single-response model limits and user cancellation remain.
The other domains retain their existing execution policies and template support.

The shared API accepts already collected JSON, JSONL and CSV snapshots, with
local evidence profiling, exact replay, nested mappings and cross-file identities.
Serve keeps its fixed source rules under `dataelf_server/ontology/templates`;
intent recognition, stage submissions and HTTP content contracts remain separate
from the AI Index CLI reviewer.

Historical outer config names still migrate with deprecation warnings; AI Index
CLI template-mode requests return an explicit migration error. Move acquisition pagination to
`domains.ai_index.source.page_size`. See [ontology documentation](dataelf/ontology/README.md)
for the public API, executable contract, configuration and migration details.

AI Index research exposes `dataelf_submit_output` with the domain's JSON schema in
the system prompt and tool declaration. The tool validates direct object arguments
and writes only declared final artifacts. Input snapshots remain protected. AI Index requires the exact declared roots
and completes only after `analysis_submit` and independent approval. In legacy
non-AI-Index paths, top-level arrays are wrapped only when a declared `json_root` specifies the exact
object envelope and all items pass validation; the original is saved in a review
audit. Ontology Stage1 supports small validated draft patches, so format retries do
not require regenerating the entire contract. This output tool is not enabled in Serve.

## Trajectory Analysis domain

The `trajectory_analysis` domain uses a domain-owned Python Client to query WT Serving read-only and identify evidence-supported deviations in agent trajectories. It owns bounded acquisition, analysis instructions, and report review; no global WT registration is required.

See the [Trajectory Analysis README](dataelf/domains/trajectory_analysis/README.md) for installation, configuration, a normal task example, artifacts, Skill/Client development, tests, and source/wheel/POSIX limits.

## Finance domain

The `finance` domain provides generic financial data-analysis capability. Data sources are composable (`source.sqlite` and `source.files`, web-only, or `source.fixture` for offline runs), and a benchmark (`ddr_10k` and others, selected via `--param benchmark=<name>`) presets the task type, tool flags, and prompt profile. The domain owns the finance tool set, output contracts, and review:

```bash
dataelf run --domain finance "Explore company financial facts and report the strongest insights"
```

See the [Finance README](dataelf/domains/finance/README.md) for installation, configuration, data sources, tool flags, benchmark presets, artifacts, and limitations.

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

Set `explorer.pi.extra_args: "--thinking off"` to disable reasoning, or use `--thinking high` to enable it. For providers registered with `compat.thinkingFormat: zai`, CLI and Serve send both `thinking.type` and `chat_template_kwargs.enable_thinking`. Reasoning blocks are excluded from subsequent CLI context; raw diagnostic events remain available.

Project Pi settings live in `.pi/settings.json`, `.pi/agent/models.json`, and `pi-harness.config.json`. `pi-harness.config.json` belongs to `@quarkos/pi-fusion` and is resolved from the configured Pi working directory.

The supported user-facing setup path is `dataelf setup`. It owns the project-local Pi/npm lifecycle and verifies the runtime before a job starts; the files above are implementation/configuration details for maintainers and advanced integrations.

DataElf selects the Explorer model through `explorer.pi.model` (a provider/model pair registered in your own Pi configuration). Pi owns the provider registry, endpoint, protocol, and model metadata in `.pi/agent/models.json`; credentials should be supplied through the local `env` mapping (for example, `OPENAI_API_KEY`) and referenced by the provider configuration. `.pi/settings.json` supplies Pi's fallback provider/model only when DataElf does not pass an explicit model.

`explorer.pi.log_mode` can be `quiet`, `summary`, or `raw`. Raw JSON events are always saved to `logs/pi_events.jsonl`; terminal verbosity does not change the artifact contract.

Official Pi CLI resource flags can still be passed through `explorer.pi.extra_args` for advanced integrations, but domain resources should normally use the directory convention above:

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

## Optional Insight HTTP API

Run `uv sync` to install the project, including its HTTP dependencies, then run `.venv/bin/python -m dataelf_server --config /path/to/dataelf.yaml` (or `.venv/bin/dataelf-serve`). The server shares the current discovery core and uses `.dataelf/server` for API attempts. See [DataElf Server](dataelf_server/README.md), [部署说明](dataelf_server/deployment/README.md), and [API 外部调用说明](dataelf_server/API_USAGE.md).
