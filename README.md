# DataElf Insight Discovery Runtime

DataElf is a user-triggered insight discovery runtime for AI science intelligence. Agent runtime and domain modeling are independent dimensions:

```text
AI Index workspace -> optional AI Index ontology/RDF modeling -> Pi or DeepAgentsCode
                   -> insights -> review/finalize
```

DataElf owns the outer workflow, workspace contract, AI Index access, and result parsing. Pi owns the agent runtime, model/provider configuration, skills, extensions, packages, tools, and execution loop.

## Quick Start

Prerequisites:

- Python 3.11+
- Node.js 22.19+ and npm

Install Python dependencies:

```bash
uv venv
uv pip install -e ".[dev]"
```

If you do not use `uv`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Install the pinned Pi CLI dependency declared in `package.json`:

```bash
npm install
```

Install or reconcile project-local Pi packages declared in `.pi/settings.json`:

```bash
PI_CODING_AGENT_DIR=.pi/agent npm_config_cache=.npm-cache \
  ./node_modules/.bin/pi install npm:@quarkos/pi-fusion --local --approve
```

Why this extra command exists: `.pi/settings.json` is committed, but generated package files under `.pi/npm/` are not. A fresh clone needs npm to populate `.pi/npm/node_modules/`. `npm_config_cache=.npm-cache` keeps npm cache writes inside the repo and avoids user-level `~/.npm` permission issues.

Create or edit local secrets/config:

```bash
dataelf init
```

`dataelf init` creates `dataelf.local.yaml` if it does not already exist. That file is ignored by git and is where each developer should put API keys.

Verify Pi package loading:

```bash
PI_CODING_AGENT_DIR=.pi/agent OPENAI_API_KEY=placeholder ./node_modules/.bin/pi list --approve
PI_CODING_AGENT_DIR=.pi/agent OPENAI_API_KEY=placeholder ./node_modules/.bin/pi --approve --list-models fusion
```

Expected signals:

```text
Project packages:
  npm:@quarkos/pi-fusion

provider  model
fusion    fusion
```

Run a discovery job:

```bash
dataelf discover "围绕 Agentic LLMs，基于 AI Index，发现最近值得关注的 3 个 insight"
```

## DataElf Config

DataElf loads config in this order:

1. Built-in defaults
2. The first existing file among `dataelf.local.yaml`, `dataelf.local.yml`, `dataelf.yaml`, `dataelf.yml`, `.dataelf/config.yaml`, `.dataelf/config.yml`, `.dataelf/config.json`
3. Environment variables, which override config file values

Use `DATAELF_CONFIG_FILE=/path/to/config.yaml` to select a specific file.

AI Index ontology/RDF 建模的完整运行、产物和排查说明见
[`dataelf/domains/ai_index/modeling/ontology/README.md`](dataelf/domains/ai_index/modeling/ontology/README.md)，代码结构与模块职责见
[`dataelf/domains/ai_index/modeling/ontology/ARCHITECTURE.md`](dataelf/domains/ai_index/modeling/ontology/ARCHITECTURE.md)。正式日志只保留模型事件、
heartbeat、耗时、重试和脱敏错误，不落盘完整 HTTP 请求/响应或 API key。

Recommended `dataelf.local.yaml` for the Pi explorer:

```yaml
# DataElf workspace and AI Index data source.
workspace_dir: .dataelf
fixtures_dir: fixtures/ai_index
ai_index_mode: api
ai_index_base_url: https://index.shlab.org.cn/api/v2
ai_index_api_key: ak_...
enable_sqlite: false

# Explorer selection.
insights_explorer: pi

# Pi runner. Leave pi_model empty to let Pi use .pi/settings.json.
pi_binary: ./node_modules/.bin/pi
pi_model:
pi_mode: json
pi_cwd: .
pi_timeout_seconds:
pi_extra_args: ""
pi_log_mode: summary

# Optional AI Index-owned modeling stage. It is independent of insights_explorer.
ai_index_modeling:
  enabled: true
  # Empty runs dynamic Stage 1; ai_index_search binds the reviewed fixed template.
  ontology_template: ai_index_search
  stage1_config: dataelf/domains/ai_index/modeling/ontology/stage1/config.yaml
  stage2_config: dataelf/domains/ai_index/modeling/ontology/stage2/config.yaml
  raw_page_size: 50
  model_name: glm-5.2-1m
  model_max_tokens:
  stage1_process_timeout_seconds: 7200
  stage1_request_timeout_seconds: 900
  stage1_request_max_retries: 3
  stage2_request_timeout_seconds: 600
  stage2_request_max_retries: 3
  stage2_total_timeout_seconds: 1800

# Child-process environment for Pi and DataElf tools.
# Shell exports with the same names override these values.
env:
  PI_CODING_AGENT_DIR: .pi/agent
  OPENAI_BASE_URL: https://token.pjlab.org.cn/v1
  OPENAI_API_KEY: sk-...

  # Optional. Only needed after loading a Pi web-search skill that uses Brave.
  # BRAVE_API_KEY: xxx
```

Config notes:

- `workspace_dir`: DataElf runtime directory. Discovery jobs are written under `.dataelf/workspaces/`.
- `fixtures_dir`: Local AI Index fixture path used when `ai_index_mode: fixture`.
- `ai_index_mode`: `api` for live AI Index OpenAPI calls, `fixture` for local fixtures.
- `ai_index_base_url`: AI Index OpenAPI base URL.
- `ai_index_api_key`: AI Index OpenAPI key.
- `enable_sqlite`: Optional job registry. Keep `false` unless you need `dataelf job ...` lookup commands.
- `insights_explorer`: Agent runtime selection: `pi` or `deepagentscode`/`dcode`.
- `ai_index_modeling.enabled`: runs AI Index acquisition and ontology/RDF modeling before the selected explorer.
- `ai_index_modeling.ontology_template`: empty runs dynamic Stage 1; `ai_index_search` skips Stage 1 model calls, validates the raw contract, binds job-local provenance, and starts Stage 2. Incompatibility fails without fallback.
- `pi_binary`: Path to the Pi CLI installed by `npm install`.
- `pi_model`: Optional per-run Pi model override. Leave empty so Pi uses `.pi/settings.json`.
- `pi_mode`: Pi output mode. DataElf expects `json`.
- `pi_cwd`: Working directory for the Pi process. Keep `.` so Pi project settings and `pi-harness.config.json` are loaded from the repo root.
- `pi_timeout_seconds`: Optional hard timeout. Empty means DataElf derives it from the job constraint.
- `pi_extra_args`: Extra official Pi CLI flags, for example `--skill /path/to/brave-search`.
- `pi_log_mode`: `summary`, `quiet`, or `raw`. Raw Pi JSON is always saved to workspace logs.
- Other `ai_index_modeling.*` fields scope Stage 1/2 config paths, model overrides, page size, timeouts, and retries to this domain feature.
- `env`: Environment forwarded to the child Pi process. Exported shell variables win over values in this file.

Environment variable override examples:

```bash
export DATAELF_AI_INDEX_MODE=fixture
export DATAELF_PI_LOG_MODE=raw
export DATAELF_AI_INDEX_MODELING_ENABLED=true
export DATAELF_AI_INDEX_MODELING_ONTOLOGY_TEMPLATE=ai_index_search
export OPENAI_API_KEY=sk-...
```

## Pi Configuration

Project-level Pi config lives in:

```text
.pi/settings.json
.pi/agent/models.json
pi-harness.config.json
```

`.pi/settings.json` is standard Pi project settings. It currently sets the default provider/model and declares the project package:

```json
{
  "defaultProvider": "openai",
  "defaultModel": "glm-5.2-1m",
  "defaultThinkingLevel": "medium",
  "retry": {
    "enabled": true,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "provider": {"timeoutMs": 900000, "maxRetries": 0}
  },
  "packages": ["npm:@quarkos/pi-fusion"]
}
```

`.pi/agent/models.json` is the project-local model registry for the configured
OpenAI-compatible endpoint. `dataelf.local.yaml` sets
`PI_CODING_AGENT_DIR: .pi/agent` so this file is visible to Pi without using the
user's global `~/.pi/agent`.

The provider entries set compatibility flags for the deployed gateway. Keep
those flags synchronized with the gateway's streaming and tool-call behavior.

`pi-harness.config.json` belongs to `@quarkos/pi-fusion`, not DataElf. Pi Fusion's extension code looks for `process.cwd()/pi-harness.config.json`; DataElf runs Pi with `pi_cwd: .`, so the file is placed at the repository root. If `pi_cwd` changes, this file must move with that cwd or Pi Fusion will fall back to its own defaults.

## Pi Packages, Extensions, And Skills

Pi packages are distribution bundles. A package can contain extensions, skills, prompt templates, and themes. Project npm packages install under `.pi/npm/`; that directory is generated and ignored by git.

`@quarkos/pi-fusion` is an extension package. Its package manifest declares:

```json
{
  "extensions": ["./index.js"]
}
```

That means it loads JavaScript runtime code into Pi. It registers:

- `/fusion <prompt>` slash command
- `deliberate` tool callable by the Pi agent
- `fusion/fusion` model provider

A Pi skill is different: it is usually a directory with `SKILL.md`, plus optional helper scripts and references. Skills mainly provide on-demand workflow instructions. Extensions provide runtime capabilities. They can work together: for example, a future DataElf skill can tell the agent when to call Fusion's `deliberate` tool during insight ranking.

## Pi Fusion In DataElf

Pi Fusion is useful as a high-quality review and deliberation layer, not as the first source of truth for AI Index data. Good uses:

- Challenge candidate insights before final selection
- Compare multiple plausible interpretations of the same signal
- Identify blind spots and missing evidence
- Rank final insight candidates with a technical expert and a skeptic persona

`pi-harness.config.json` currently uses:

```json
{
  "mode": "3x",
  "provider": "openai"
}
```

`3x` means two parallel expert calls plus one synthesis call. Pi Fusion's full `5x` mode uses three panel experts, a judge, and a synthesis model. `3x` is cheaper and faster, so it is the right first test mode for DataElf.

The safest first way to use Fusion inside DataElf is to ask the Pi explorer to use the registered `deliberate` tool only at the final ranking/review step:

```bash
dataelf discover "围绕 Agentic LLMs，基于 AI Index，发现最近值得关注的 3 个 insight。若 Pi runtime 暴露 deliberate 工具，请在最终筛选 3 个 insight 前调用它，对候选 insight 做反方论证、盲点检查和排序建议。"
```

Do not make `fusion/fusion` the default model for the whole DataElf job yet. That routes every agent turn through the deliberation pipeline and may conflict with DataElf's artifact-writing contract. Use the `deliberate` tool first, then promote deeper integration after benchmark runs.

## Logs

Each Pi job writes:

```text
.dataelf/workspaces/<job_id>/logs/pi_events.jsonl
.dataelf/workspaces/<job_id>/logs/pi_stdout.log
.dataelf/workspaces/<job_id>/logs/pi_stderr.log
.dataelf/workspaces/<job_id>/logs/pi_command.json
.dataelf/workspaces/<job_id>/logs/pi_env_redacted.json
.dataelf/workspaces/<job_id>/logs/pi_model_events.jsonl
```

`pi_model_events.jsonl` is created when the selected compatibility transport
supports it. It contains request start/heartbeat/finish/error metadata only;
full model payloads and responses are not persisted.

`pi_log_mode` controls terminal output:

- `summary`: compact event summaries, suitable for normal runs
- `quiet`: no streamed Pi events in terminal
- `raw`: mirror raw Pi JSON event stream to terminal

The old `pi_stream_logs` boolean is still accepted for compatibility: `true` maps to `raw`, and `false` maps to `quiet`, unless `pi_log_mode` is set.

## Web Search

Pi web search should be added through Pi skills or extensions, not hard-coded into the DataElf Python runner. One candidate is the community `brave-search` skill:

```bash
git clone https://github.com/badlogic/pi-skills /path/to/pi-skills
cd /path/to/pi-skills/brave-search && npm install
```

Then load it using official Pi mechanisms, for example:

```yaml
pi_extra_args: "--skill /path/to/pi-skills/brave-search"
env:
  BRAVE_API_KEY: xxx
```

Leaving `BRAVE_API_KEY` unset is fine as long as the Brave skill is not loaded.

## Discovery Workspace

Each job creates:

```text
.dataelf/workspaces/<job_id>/
  raw/ai_index/
  raw/web/
  tables/
  scripts/
  notes/
  deep_dives/
  insights/
  prompts/
  logs/
  reviews/
```

Key files:

```text
insights/candidate_signals.json
insights/insight_candidates.json
insights/final_brief.md
prompts/discovery_prompt.md
logs/pi_events.jsonl
logs/pi_stdout.log
logs/pi_stderr.log
reviews/quality_review.json
workspace_index.json
```

AI Index scripts can import:

```python
from dataelf.domains.ai_index.client import AIIndexClient

client = AIIndexClient.from_env()
papers = client.search_papers(sub_domains=["Agentic LLMs"], sort_type="heat", page=1, size=50)
papers_df = client.to_dataframe("papers", papers)
client.save_table("papers", papers_df)
client.save_raw("papers_agentic_llms_page_1", papers)
```

## AI Index API

The AI Index connector supports:

- `POST /openapi/paper/search`
- `POST /openapi/institutions/search`
- `POST /openapi/scholar/search`
- `GET /openapi/institutions/:institution_id/funding-profile`

The default production base URL in code is `https://index.shlab.org.cn/api/v2`; override it with `AI_INDEX_BASE_URL` or `ai_index_base_url`.

## Tests

```bash
.venv/bin/python -m pytest -q
```
