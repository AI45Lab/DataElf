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

SQLite job registry is disabled by default. M1 uses workspace files as the source of truth. Enable SQLite only if you need `dataelf job ...` lookup commands:

```bash
export DATAELF_ENABLE_SQLITE=1
```

To force fixture mode for local tests:

```bash
export DATAELF_AI_INDEX_MODE="fixture"
```

To override the live AI Index OpenAPI target:

```bash
export DATAELF_AI_INDEX_MODE="api"
export AI_INDEX_BASE_URL="https://index.shlab.org.cn/api/v2"
export AI_INDEX_API_KEY="..."
```

DeepAgentsCode CLI is the default explorer for `dataelf discover`:

```bash
export DATAELF_INSIGHTS_EXPLORER="deepagentscode"  # optional; this is the default
curl -LsSf https://langch.in/dcode | bash
export DATAELF_DCODE_BINARY="dcode"  # optional; defaults to dcode
export DATAELF_DCODE_SHELL_ALLOW_LIST="all"  # optional; defaults to all for M1 testing
export DATAELF_DCODE_EXTRA_ARGS="--max-turns 40"  # optional; appended before -n
export DATAELF_MODEL="openai:gpt-5.5"  # optional; if unset, dcode uses its own default model config
export TAVILY_API_KEY="..."  # optional, enables dcode web_search/fetch_url
```

Configure LLM provider credentials in DeepAgentsCode or in the shell environment before running DataElf. DataElf forwards the current environment to the child process, but it does not own provider auth. If `DATAELF_MODEL` is set, DataElf passes it to `dcode --model`; otherwise dcode uses its own default model config. For example, use `dcode auth set openai` or export provider variables such as `OPENAI_API_KEY` / `OPENAI_BASE_URL` according to your DeepAgentsCode provider setup.

If `dcode` is not installed or not on `PATH`, DataElf fails clearly and writes details to `workspace/logs/dcode_stderr.log`.

Pi CLI can be used as a parallel explorer:

```bash
npm install
export DATAELF_INSIGHTS_EXPLORER="pi"
export DATAELF_PI_BINARY="./node_modules/.bin/pi"  # optional; DataElf checks this path before PATH
export DATAELF_PI_MODEL="openai/gpt-4o"  # optional; if unset, pi uses its own default model config
export OPENAI_API_KEY="..."  # or configure Pi provider auth in the official Pi way
dataelf discover "围绕 Agentic LLMs，基于 AI Index 和联网搜索，发现最近值得关注的 3 个 insight"
```

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
export DATAELF_PI_BRAVE_SEARCH_SKILL_PATH="/path/to/pi-skills/brave-search"
# or export DATAELF_PI_SKILL_PATHS="/path/to/pi-skills/brave-search"
```

DataElf passes configured skill paths to `pi --skill ...`; Pi remains responsible for loading and executing the skill.

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
