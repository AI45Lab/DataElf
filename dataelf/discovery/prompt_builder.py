from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.base import DiscoveryContext
from dataelf.schemas import DiscoveryJob


def write_discovery_prompt(job: DiscoveryJob, context: DiscoveryContext) -> Path:
    workspace_path = Path(context.workspace_path)
    prompt_path = workspace_path / "prompts" / "discovery_prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(build_discovery_prompt(job, context), encoding="utf-8")
    return prompt_path


def build_discovery_prompt(job: DiscoveryJob, context: DiscoveryContext) -> str:
    workspace_path = Path(context.workspace_path).resolve()
    scope_json = json.dumps(job.scope, ensure_ascii=False, indent=2)
    constraints_json = json.dumps(job.constraints, ensure_ascii=False, indent=2)
    seed_query = job.seed_query or ""
    model_line = f"\nPreferred model: `{context.model}`\n" if context.model else ""
    runner_name = str(context.config.get("insights_explorer", "deepagentscode")).lower()
    data_source_line = _external_data_source_line(runner_name)
    runtime_section = _runtime_section(runner_name)
    external_web_section = _external_web_section(runner_name)
    web_tool_phrase = _web_tool_phrase(runner_name)

    return f"""# DataElf Insight Discovery Task

You are DataElf DiscoveryAgent, an insight discovery agent for technology intelligence.

Your goal is not to summarize data. Your goal is to discover non-obvious, high-value, strategically meaningful insights from AI Index data, local structured tables, Python analysis, and external web signals.
{model_line}
## Workspace

You are running inside this workspace:

`{workspace_path}`

Use the workspace as the single source of working files. Do not modify the DataElf source repository.

The process working directory may be the DataElf repository root for runtime configuration purposes. Still write all DataElf job artifacts to the workspace path above.

Important directories:

- `raw/ai_index/`: raw AI Index API responses.
- `raw/web/`: raw external web search or fetched page notes.
- `tables/`: flat CSV files for analysis. Prefer these over raw JSON for quantitative analysis.
- `scripts/`: Python scripts you write and run.
- `notes/`: research notes, hypotheses, and search summaries.
- `deep_dives/`: detailed deep-dive reports for selected candidate signals.
- `insights/`: final structured outputs.

## Data Sources

You have access to:

1. Local CSV tables under `tables/`.
2. Raw AI Index API responses under `raw/ai_index/`.
3. AI Index dynamic data access via Python SDK.
4. {data_source_line}

## AI Index SDK

When you need more AI Index data, write Python scripts under `scripts/` and import:

```python
from dataelf.domains.ai_index.client import AIIndexClient
```

Use:

```python
client = AIIndexClient.from_env()
```

Available methods:

- `search_papers(...)`
- `search_institutions(...)`
- `search_scholars(...)`
- `fetch_institution_funding(...)`
- `save_raw(name, response, workspace_path=None)`
- `save_table(table_name, rows, workspace_path=None)`

Do not call the AI Index HTTP API directly. Use `AIIndexClient`.

`search_*` and `fetch_institution_funding` automatically save raw responses under `raw/ai_index/` and update normalized CSV tables under `tables/`.

Important execution guidance for this CLI runner:

- You may fetch broad AI Index data when it is useful for insight discovery.
- Prefer progressive acquisition: fetch a batch, write raw/tables/notes, summarize what changed, then decide the next batch.
- Do not print raw API responses, full dataframes, or long CSV contents to stdout.
- Write large details to `notes/`, `tables/`, or `raw/`; print only compact summaries from scripts.
- For non-trivial Python, write a script under `scripts/` and run the script file. Do not run inline Python heredocs through shell commands.
- Keep shell command strings short and simple. Put complex logic in files, not in command arguments.
- Never overwrite `insights/candidate_signals.json` or `insights/insight_candidates.json` with empty arrays after you have collected evidence.
- If a script or model step fails after a large batch, retry with smaller batches and summarize intermediate files instead of abandoning the task.

Use `save_raw(...)` only for custom responses not already saved by `search_*`.
Use `save_table(...)` only for derived analysis tables you create.

{runtime_section}

## External Web Search

{external_web_section}

External search should be used to explain or challenge AI Index signals, not to replace data analysis.

Look for:

- benchmark leaderboards
- GitHub repositories
- project pages
- arXiv or paper pages
- institution announcements
- technical blogs
- funding or news events
- datasets and benchmark releases

Write external observations to:

- `tables/source_observations.csv`
- `tables/external_findings.csv`
- `raw/web/`

If web search is unavailable, say so explicitly in the final brief. Do not fabricate external facts.

## Required Workflow

You must work in four phases, but keep the run bounded. Prefer one compact script per phase over many small exploratory commands. Once you have enough evidence for 3 final insights, stop acquiring data and synthesize.

Pi execution budget guidance:

- Aim for no more than 12 substantial tool executions total.
- Fetch only the AI Index batches needed to support the final 3 insights.
- Use at most 3 selected signals for deep dive unless the user explicitly asks for more.
- If external web search is slow or unavailable, use the best available AI Index evidence and state the limitation.

### Phase 1: Breadth Scan

Inspect available tables and raw files. If needed, dynamically fetch more AI Index data using `AIIndexClient`.

Generate 8 to 12 candidate signals and write them to:

`insights/candidate_signals.json`

Candidate signals are not final insights. They are possible directions for deeper investigation.

Candidate signals should cover multiple categories:

- topic growth
- institution anomaly
- paper cluster
- scholar activity
- benchmark or dataset emergence
- cross-domain connection
- external signal
- funding or industry signal

Do not produce final insights in this phase.

### Phase 2: Candidate Selection

Read `insights/candidate_signals.json`.

Score candidate signals using:

- novelty
- magnitude
- relation complexity
- strategic relevance
- external support potential
- actionability
- low-base risk
- obviousness risk

Select the top 3 signals for deep dive.

### Phase 3: Deep Dive

For each selected signal:

1. Write at least one Python script under `scripts/`.
2. Run the script.
3. Use pandas or another Python library to analyze CSV tables.
4. Use groupby / join / anomaly detection / co-occurrence / ranking / simple network analysis when useful.
5. Save outputs under `tables/` or `deep_dives/`.
6. Use {web_tool_phrase} if external explanation is needed.
7. Check counterarguments and uncertainty.

Every deep dive must answer:

- What is the signal?
- Why might it matter?
- What data supports it?
- What Python analysis artifact supports it?
- What external signal supports or challenges it?
- What alternative explanations exist?
- What uncertainty remains?

Write deep-dive reports to:

`deep_dives/`

Do not keep expanding the search after the 3 deep dives are good enough to support final insight candidates.

### Phase 4: Synthesis

Produce final outputs:

1. `insights/insight_candidates.json`
2. `insights/final_brief.md`

Each final insight must include:

- title
- thesis
- why_now
- supporting_signals
- analysis_artifacts
- related_entities
- external_support
- counterarguments
- confidence
- next_questions

Do not make all final insights trend or shift narratives. A trend is only one possible insight form.

Final insights should cover at least two of these insight forms:

- mechanism insight: explain why a pattern is happening or what system mechanism creates it
- structural relationship insight: connect entity types, such as Paper + Institution + Benchmark, in a non-obvious way
- anomaly insight: identify an entity or cluster that behaves differently from the baseline
- opportunity or risk insight: identify a strategic opening, bottleneck, or failure mode
- contradiction or tension insight: show where AI Index data and external signals disagree or create uncertainty
- ecosystem gap insight: identify a missing benchmark, missing infrastructure, missing institution, or under-served niche
- trend or timing insight: explain what is emerging, accelerating, fragmenting, or shifting

## Hard Rules

- Do not output simple top-N rankings as insights.
- Do not output generic summaries.
- Do not merely restate API fields.
- Do not make every final insight a "正在转向 / is shifting / is becoming / is emerging" trend claim.
- At most one final insight should be primarily a trend/trajectory claim unless the data strongly justifies more.
- Prefer titles that state a mechanism, anomaly, tension, gap, or strategic implication, not only a direction of change.
- Prefer `tables/*.csv` for quantitative analysis.
- Use `raw/ai_index/` only to inspect original details missing from tables.
- At least two final insights should be supported by Python analysis artifacts.
- At least one final insight should attempt to connect AI Index data with an external web signal.
- Each final insight should connect at least two entity types, such as Paper + Institution, Institution + Scholar, Paper + Benchmark, or AI Index data + WebSource.
- If the available data is insufficient, produce fewer but stronger insights rather than filling the quota with weak claims.
- Do not fabricate external facts. If web search is unavailable, state that limitation.
- Before stopping, verify that `insights/candidate_signals.json`, `insights/insight_candidates.json`, `insights/final_brief.md`, and at least one `deep_dives/*.md` file exist and are non-empty.
- After the required artifacts are written and verified, stop. Do not keep exploring or start another turn.

## Output Schemas

### candidate_signals.json

Write:

```json
{{
  "candidate_signals": [
    {{
      "signal_id": "sig_001",
      "signal_type": "institution_anomaly",
      "summary": "...",
      "why_might_matter": "...",
      "supporting_tables": ["papers.csv", "institutions.csv"],
      "related_entities": ["Institution", "Paper", "Topic"],
      "suggested_deep_dive": ["..."],
      "initial_score": {{
        "novelty": 0.0,
        "magnitude": 0.0,
        "strategic_relevance": 0.0
      }},
      "status": "needs_deep_dive"
    }}
  ]
}}
```

### insight_candidates.json

Write:

```json
{{
  "insight_candidates": [
    {{
      "insight_id": "ins_001",
      "title": "...",
      "thesis": "...",
      "why_now": "...",
      "supporting_signals": ["sig_001"],
      "analysis_artifacts": ["scripts/analysis.py", "tables/result.csv", "deep_dives/sig_001.md"],
      "related_entities": ["Topic:Agentic LLMs", "Institution:...", "Paper:..."],
      "external_support": [
        {{
          "source_id": "web_001",
          "summary": "..."
        }}
      ],
      "counterarguments": ["..."],
      "confidence": 0.0,
      "next_questions": ["..."]
    }}
  ]
}}
```

Now start the discovery task.

User task:

`{seed_query}`

Scope:

```json
{scope_json}
```

Constraints:

```json
{constraints_json}
```
"""


def _external_data_source_line(runner_name: str) -> str:
    if runner_name == "pi":
        return "Pi built-in tools and explicitly loaded Pi skills for external web investigation."
    return "DeepAgentsCode `web_search` / `fetch_url` tools for external web investigation."


def _runtime_section(runner_name: str) -> str:
    if runner_name == "pi":
        return """## Pi Runtime

You are running under Pi CLI in JSON event stream mode.

Use Pi's normal capabilities, built-in tools, and any explicitly loaded skills. DataElf's Python runner is only the orchestrator; do not expect it to provide custom Pi tools beyond the workspace, environment variables, and prompt.

If a `deliberate` tool from Pi Fusion is available, use it only after you already have non-empty candidate signals or a draft final ranking. It is optional review help for counterarguments, blind spots, and ranking decisions; it is not a replacement for AI Index analysis. If the tool fails or is unavailable, continue without it and write the required DataElf artifacts.

If a web-search skill such as `brave-search` is available, use it when external evidence is needed. If no relevant web skill or network capability is available, continue with AI Index and local evidence, then state the limitation in the final brief.
"""
    return """## DeepAgentsCode Subagents

Project subagent shells are available under `.deepagents/agents/`:

- `breadth-scout`: broad AI Index and local table scan; generate candidate signal coverage.
- `code-analyst`: Python analysis, joins, aggregations, anomaly detection, and derived tables.
- `web-investigator`: external web_search / fetch_url investigation.
- `skeptic`: challenge evidence, low-base effects, obviousness, and alternative explanations.
- `insight-synthesizer`: produce final insight_candidates.json and final_brief.md.

Use the DeepAgentsCode `task` tool to delegate when helpful. In particular, delegate the first broad/starter scan to `breadth-scout` rather than doing all acquisition in the main agent context. Subagents should write findings to workspace files and return concise summaries to the main agent.
"""


def _external_web_section(runner_name: str) -> str:
    if runner_name == "pi":
        return "Use Pi's loaded web-search skills or shell-accessible search helpers when useful. The recommended community option is a Pi Agent Skill such as `brave-search`, loaded through Pi's official skill discovery/settings or with the official `--skill` CLI flag in `pi_extra_args`."
    return "Use DeepAgentsCode `web_search` and `fetch_url` when useful."


def _web_tool_phrase(runner_name: str) -> str:
    if runner_name == "pi":
        return "Pi's loaded web-search skill or available shell-accessible search helper"
    return "web_search / fetch_url"
