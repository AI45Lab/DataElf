# DataElf M1 Insight Discovery Runtime

DataElf M1 is a user-triggered Insight Discovery runtime for AI science intelligence.

```text
dataelf discover
  -> DiscoveryJob
  -> DiscoveryWorkflow
  -> AI Index domain pack
  -> job workspace
  -> insights_explore runner (DeepAgentsCode CLI by default, Pi CLI optional)
  -> raw AI Index responses + CSV tables
  -> candidate_signals.json / insight_candidates.json / final_brief.md
```

The default `insights_explore` uses a DeepAgentsCode CLI runner. A parallel Pi CLI runner is also available for testing Pi as the explorer agent. Both runners use the same stable outer contract: `DiscoveryWorkflow`, `DiscoveryJob`, workspace layout, and `insight_candidates.json` schema.

## Setup

```bash
uv venv
uv pip install -e ".[dev]"
```

Live AI Index API mode is the default. The production base URL and the tested API key are built into the M1 config from the provided curl, so interns do not need extra AI Index exports for the default path.

Run `dataelf init` once to create `dataelf.local.yaml` in the project root. DataElf loads config in this order:

1. Built-in defaults
2. The first existing config file among `dataelf.local.yaml`, `dataelf.local.yml`, `dataelf.yaml`, `dataelf.yml`, `.dataelf/config.yaml`, `.dataelf/config.yml`, `.dataelf/config.json`
3. Environment variables, which override the config file

Use `DATAELF_CONFIG_FILE=/path/to/config.yaml` to select a specific file.

Example `dataelf.local.yaml`:

```yaml
workspace_dir: .dataelf
fixtures_dir: fixtures/ai_index
model: openai:gpt-5.5
ai_index_mode: api
ai_index_base_url: https://index.shlab.org.cn/api/v2
ai_index_api_key: ...
enable_sqlite: false
insights_explorer: deepagentscode
dcode_binary: dcode
dcode_shell_allow_list: all
dcode_extra_args: "--max-turns 40"
dcode_stream_logs:
pi_binary: ./node_modules/.bin/pi
pi_model: openai/gpt-4o
pi_mode: json
pi_cwd: .
pi_timeout_seconds:
pi_extra_args: ""
env:
  PI_CODING_AGENT_DIR: .pi/agent
  OPENAI_API_KEY: ...
  TAVILY_API_KEY: ...
  # BRAVE_API_KEY: ...
```

SQLite job registry is disabled by default. M1 uses workspace files as the source of truth. Set `enable_sqlite: true` only if you need `dataelf job ...` lookup commands.

To force fixture mode for local tests, set `ai_index_mode: fixture` in the config file, or override it for one shell:

```bash
export DATAELF_AI_INDEX_MODE="fixture"
```

To override the live AI Index OpenAPI target for one shell:

```bash
export DATAELF_AI_INDEX_MODE="api"
export AI_INDEX_BASE_URL="https://index.shlab.org.cn/api/v2"
export AI_INDEX_API_KEY="..."
```

DeepAgentsCode CLI is the default explorer for `dataelf discover`:

```yaml
insights_explorer: deepagentscode
model: openai:gpt-5.5
dcode_binary: dcode
dcode_shell_allow_list: all
dcode_extra_args: "--max-turns 40"
env:
  TAVILY_API_KEY: ...
```

You can still override dcode values for one shell with `DATAELF_DCODE_BINARY`, `DATAELF_DCODE_SHELL_ALLOW_LIST`, `DATAELF_DCODE_EXTRA_ARGS`, and provider/search keys such as `TAVILY_API_KEY`.

Configure LLM provider credentials in DeepAgentsCode or in the shell environment before running DataElf. DataElf forwards the current environment to the child process, but it does not own provider auth. If `DATAELF_MODEL` is set, DataElf passes it to `dcode --model`; otherwise dcode uses its own default model config. For example, use `dcode auth set openai` or export provider variables such as `OPENAI_API_KEY` / `OPENAI_BASE_URL` according to your DeepAgentsCode provider setup.

If `dcode` is not installed or not on `PATH`, DataElf fails clearly and writes details to `workspace/logs/dcode_stderr.log`.

Pi CLI can be used as a parallel explorer:

```yaml
insights_explorer: pi
pi_binary: ./node_modules/.bin/pi
pi_model:
pi_mode: json
pi_cwd: .
pi_extra_args: ""
env:
  PI_CODING_AGENT_DIR: .pi/agent
  OPENAI_API_KEY: ...
```

Then install the pinned Pi CLI dependency:

```bash
npm install
dataelf discover "围绕 Agentic LLMs，基于 AI Index 和联网搜索，发现最近值得关注的 3 个 insight"
```

Configure Pi provider auth, settings, packages, skills, and extensions in the official Pi way. DataElf only starts Pi as a child process and collects workspace artifacts.

Recommended Pi model setup for this project:

```text
.pi/settings.json          project-level Pi settings, such as defaultProvider/defaultModel
.pi/agent/models.json      Pi agent-dir models.json for custom OpenAI-compatible providers
dataelf.local.yaml env     secrets and env values referenced by Pi config, such as OPENAI_API_KEY
```

Leave `pi_model` empty when you want Pi to use `.pi/settings.json`. Setting `pi_model` makes DataElf pass `--model ...`, which overrides Pi's default model selection for that run.

Pi runner boundary:

- DataElf starts `pi --mode json --no-session --approve ...` and passes `pi_extra_args` verbatim as official Pi CLI flags.
- Pi owns settings, provider auth, packages, skills, extensions, tools, and prompt/runtime behavior.
- DataElf runs Pi with `cwd` set to `pi_cwd`, which defaults to the repository root (`.`), so repository-level `.pi/settings.json` and `.pi/` resources can be prepared before a job starts.
- The generated job workspace is passed separately through `DATAELF_WORKSPACE` / `DATAELF_JOB_WORKSPACE`; the prompt tells Pi to write required artifacts there.
- Reusable Pi tuning can live in official global Pi locations such as `~/.pi/agent/settings.json` / `~/.pi/agent/skills/`, repository-local `.pi/`, or official CLI flags through `pi_extra_args`.
- `PI_CODING_AGENT_DIR` is optional; this repo sets it to `.pi/agent` in `dataelf.local.yaml` so DataElf-specific `models.json` and agent-dir resources are visible in the project instead of hidden in the user's home directory.

Pi runs in official JSON event stream mode and DataElf writes:

```text
logs/pi_events.jsonl
logs/pi_stdout.log
logs/pi_stderr.log
logs/pi_command.json
logs/pi_env_redacted.json
```

For web search with Pi, use Pi skills instead of adding search logic to DataElf's Python runner. One clean option is the community `brave-search` skill:

```bash
git clone https://github.com/badlogic/pi-skills /path/to/pi-skills
cd /path/to/pi-skills/brave-search && npm install
export BRAVE_API_KEY="..."
```

Then load it using Pi's official mechanisms, for example global Pi settings/skills or the official CLI flag in DataElf config:

```yaml
pi_extra_args: "--skill /path/to/pi-skills/brave-search"
```

## Run

```bash
dataelf init
dataelf discover "围绕 Agentic LLMs，基于 AI Index 和联网搜索，发现最近值得关注的 3 个 insight"
# With DATAELF_ENABLE_SQLITE=1 only:
# dataelf job workspace <job_id>
# dataelf job insights <job_id>
# dataelf job brief <job_id>
# dataelf job review <job_id>
# dataelf job logs <job_id>
```

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
  .deepagents/agents/
```

Key files:

```text
insights/candidate_signals.json
insights/insight_candidates.json
insights/final_brief.md
prompts/discovery_prompt.md
logs/dcode_stdout.log
logs/dcode_stderr.log
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

The default production base URL in code is `https://index.shlab.org.cn/api/v2`; override it with `AI_INDEX_BASE_URL`.

## Team Handoff

- Intern A can focus on `dataelf/discovery/prompt_builder.py` and dcode-native config. DataElf scaffolds `.deepagents/agents/*/AGENTS.md` only when missing, so workspace-level agent edits are not overwritten on reruns.
- Intern B can focus on `dataelf/domains/ai_index/domain.yaml`, `table_builder.py`, and future mapper/normalizer modules.
