# DataElf Insight Discovery Runtime

DataElf is a user-triggered insight discovery runtime for AI science intelligence. The current recommended explorer path is:

```text
dataelf discover
  -> DiscoveryJob
  -> AI Index domain pack
  -> job workspace under .dataelf/workspaces/<job_id>/
  -> Pi CLI explorer
  -> raw AI Index responses + CSV tables
  -> candidate_signals.json / insight_candidates.json / final_brief.md
```

DataElf owns the outer workflow, workspace contract, AI Index access, and result parsing. Pi owns the agent runtime, model/provider configuration, skills, extensions, packages, tools, and execution loop.

## Quick Start

Prerequisites:

- Python 3.11+
- Node.js and npm

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

Install or reconcile project-local Pi packages declared in `.pi/settings.json` when the project enables any Pi packages:

```bash
PI_CODING_AGENT_DIR=.pi/agent npm_config_cache=.npm-cache \
  ./node_modules/.bin/pi install npm:<package-name> --local --approve
```

Why this extra command may be needed: `.pi/settings.json` is committed, but generated package files under `.pi/npm/` are not. A fresh clone needs Pi to populate `.pi/npm/node_modules/` for any declared Pi packages. `npm_config_cache=.npm-cache` keeps npm cache writes inside the repo and avoids user-level `~/.npm` permission issues. The baseline repo currently does not require a project Pi package.

Create or edit local secrets/config:

```bash
dataelf init
```

`dataelf init` creates `dataelf.local.yaml` if it does not already exist. That file is ignored by git and is where each developer should put API keys.

Verify Pi config and package loading:

```bash
PI_CODING_AGENT_DIR=.pi/agent OPENAI_API_KEY=placeholder ./node_modules/.bin/pi list --approve
```

With no project packages enabled, the package list can be empty. After installing a web-search package, it should appear here.

You can also check the configured custom model:

```bash
PI_CODING_AGENT_DIR=.pi/agent OPENAI_API_KEY=placeholder ./node_modules/.bin/pi --approve --list-models gpt-5.5
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

# Child-process environment for Pi and DataElf tools.
# Shell exports with the same names override these values.
env:
  PI_CODING_AGENT_DIR: .pi/agent
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
- `insights_explorer`: Use `pi` for the Pi-based explorer.
- `pi_binary`: Path to the Pi CLI installed by `npm install`.
- `pi_model`: Optional per-run Pi model override. Leave empty so Pi uses `.pi/settings.json`.
- `pi_mode`: Pi output mode. DataElf expects `json`.
- `pi_cwd`: Working directory for the Pi process. Keep `.` so Pi project settings are loaded from the repo root.
- `pi_timeout_seconds`: Optional hard timeout. Empty means DataElf derives it from the job constraint.
- `pi_extra_args`: Extra official Pi CLI flags, for example `--skill /path/to/brave-search`.
- `pi_log_mode`: `summary`, `quiet`, or `raw`. Raw Pi JSON is always saved to workspace logs.
- `env`: Environment forwarded to the child Pi process. Exported shell variables win over values in this file.

Environment variable override examples:

```bash
export DATAELF_AI_INDEX_MODE=fixture
export DATAELF_PI_LOG_MODE=raw
export OPENAI_API_KEY=sk-...
```

## Pi Configuration

Project-level Pi config lives in:

```text
.pi/settings.json
.pi/agent/models.json
```

`.pi/settings.json` is standard Pi project settings. It currently sets the default provider/model and has no project package enabled:

```json
{
  "defaultProvider": "boyuerich-openai",
  "defaultModel": "gpt-5.5",
  "defaultThinkingLevel": "medium",
  "packages": []
}
```

`.pi/agent/models.json` is the Pi agent-dir model registry for the custom OpenAI-compatible Boyuerich provider. `dataelf.local.yaml` sets `PI_CODING_AGENT_DIR: .pi/agent` so this file is visible to Pi without using the user's global `~/.pi/agent`.

The Boyuerich provider entry sets `compat.supportsUsageInStreaming: false`. Keep that unless the provider changes its stream format: Pi's OpenAI-completions adapter expects normal streaming choice deltas, while this endpoint may emit usage-only chunks that otherwise make Pi fail before DataElf can parse the workspace artifacts.

## Pi Packages, Extensions, And Skills

Pi packages are distribution bundles. A package can contain extensions, skills, prompt templates, and themes. Project npm packages install under `.pi/npm/`; that directory is generated and ignored by git.

A Pi skill is usually a directory with `SKILL.md`, plus optional helper scripts and references. Skills mainly provide on-demand workflow instructions. Extensions provide runtime capabilities by registering tools, commands, providers, or hooks with Pi.

DataElf does not hard-code third-party Pi packages. Add them through official Pi package management and keep package-specific configuration in official Pi locations or in `pi_extra_args`.

## Insight Discovery Four-Phase Flow

The inner `insights_explore` prompt asks the explorer agent to follow a bounded four-phase research flow. This flow is prompt-level behavior, while DataElf's Python workflow remains responsible for job lifecycle, workspace creation, artifact parsing, quality review, and finalization.

### Phase 1: Breadth Scan

Inspect existing tables and raw files. If needed, fetch more AI Index data through `AIIndexClient`. Generate 8 to 12 candidate signals in `insights/candidate_signals.json`; these are possible directions, not final insights.

Candidate signal coverage should include multiple categories such as topic growth, institution anomaly, paper cluster, scholar activity, benchmark or dataset emergence, cross-domain connection, external signal, funding, or industry signal.

### Phase 2: Candidate Selection

Read `insights/candidate_signals.json`, score candidate signals for novelty, magnitude, relation complexity, strategic relevance, external support potential, actionability, low-base risk, and obviousness risk, then select the top 3 signals for deep dive.

### Phase 3: Deep Dive

For each selected signal, write and run at least one Python analysis script under `scripts/`, analyze `tables/*.csv`, save derived outputs under `tables/` or `deep_dives/`, use loaded web-search tools when external explanation is needed, and explicitly check counterarguments and uncertainty.

Each deep-dive report under `deep_dives/` should answer what the signal is, why it matters, what data supports it, what Python artifact supports it, what external evidence supports or challenges it, what alternative explanations exist, and what uncertainty remains.

### Phase 4: Synthesis

Write `insights/insight_candidates.json` and `insights/final_brief.md`. Final insights must include title, thesis, why-now, supporting signals, analysis artifacts, related entities, external support, counterarguments, confidence, and next questions.

Final insights should avoid simple top-N rankings and generic summaries. Prefer mechanism, structural relationship, anomaly, opportunity/risk, contradiction/tension, ecosystem gap, or timing insights. Before stopping, the explorer must verify that candidate signals, final insight candidates, final brief, and at least one non-empty deep-dive report exist.

## Logs

Each Pi job writes:

```text
.dataelf/workspaces/<job_id>/logs/pi_events.jsonl
.dataelf/workspaces/<job_id>/logs/pi_stdout.log
.dataelf/workspaces/<job_id>/logs/pi_stderr.log
.dataelf/workspaces/<job_id>/logs/pi_command.json
.dataelf/workspaces/<job_id>/logs/pi_env_redacted.json
```

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
