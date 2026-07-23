from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from dataelf.cli import app
from dataelf.config import DEFAULT_AI_INDEX_API_KEY, DEFAULT_AI_INDEX_BASE_URL, DEFAULT_AI_INDEX_MODE, DEFAULT_INSIGHTS_EXPLORER, DataElfConfig, write_config_template
from dataelf.discovery.base import DiscoveryContext
from dataelf.discovery.deepagents_code_cli_explorer import DEFAULT_DCODE_EXTRA_ARGS, DEFAULT_SHELL_ALLOW_LIST, DeepAgentsCodeCliInsightsExplorer
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.explorer_factory import normalize_insights_explorer_name
from dataelf.discovery.pi_cli_explorer import PI_BINARY_NOT_FOUND, PiCliInsightsExplorer, _summarize_pi_event
from dataelf.discovery.quality_review import review_workspace
from dataelf.discovery.workflow import run_discovery
from dataelf.discovery.workspace import prepare_workspace
from dataelf.domains.ai_index.client import AIIndexClient
from dataelf.domains.ai_index.connector import AIIndexConnector, AI_INDEX_ENDPOINTS
from dataelf.domains.ai_index.table_builder import read_table, update_tables_from_response
from dataelf.stores.sqlite_store import SQLiteStore
from dataelf.schemas import DiscoveryJob


def _write_fake_dcode(tmp_path: Path) -> Path:
    path = tmp_path / "fake_dcode"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
mkdir -p insights scripts deep_dives tables raw/web logs
echo "App: fake | Agent: agent | Model: fake-model | Thread: fake"
cat > insights/candidate_signals.json <<'JSON'
{
  "candidate_signals": [
    {
      "signal_id": "sig_001",
      "signal_type": "institution_anomaly",
      "summary": "OpenAgent Lab shows unusual AI agent momentum across papers and funding context.",
      "why_might_matter": "The signal links institution momentum with research output and external validation needs.",
      "supporting_tables": ["papers.csv", "institutions.csv", "funding_summary.csv"],
      "related_entities": ["Institution", "Paper", "FundingEvent"],
      "suggested_deep_dive": ["Join institution heat with paper clusters"],
      "initial_score": {"novelty": 0.7, "magnitude": 0.8, "strategic_relevance": 0.75},
      "status": "needs_deep_dive"
    }
  ]
}
JSON
cat > insights/insight_candidates.json <<'JSON'
{
  "insight_candidates": [
    {
      "insight_id": "ins_001",
      "title": "Agentic LLM momentum is clustering around execution-capable institutions",
      "thesis": "The signal combines AI Index paper activity, institution momentum, and a checked external source placeholder rather than a simple ranking.",
      "why_now": "Recent workspace tables and external validation notes appear together in this run.",
      "supporting_signals": ["sig_001"],
      "analysis_artifacts": ["scripts/analyze_signal.py", "deep_dives/sig_001.md"],
      "related_entities": ["Topic:Agentic LLMs", "Institution:OpenAgent Lab", "Paper:Agent Benchmarks"],
      "external_support": [{"source_id": "web_001", "summary": "External search placeholder for fake dcode test."}],
      "counterarguments": ["The fake runner does not prove external adoption."],
      "confidence": 0.61,
      "next_questions": ["Validate benchmark adoption with live web search."]
    }
  ]
}
JSON
cat > insights/final_brief.md <<'MD'
# Insight Discovery Brief

Fake DeepAgentsCode output for tests.
MD
cat > scripts/analyze_signal.py <<'PY'
print("analysis artifact")
PY
cat > deep_dives/sig_001.md <<'MD'
# sig_001

Deep dive artifact for fake dcode.
MD
cat > tables/external_findings.csv <<'CSV'
finding_id,source_id,finding_type,summary,supports,challenges,confidence,url,source_raw
finding_001,web_001,web_search,External fake finding,ins_001,,0.5,https://example.com,
CSV
echo "fake dcode completed"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_failed_fake_dcode(tmp_path: Path) -> Path:
    path = tmp_path / "failed_fake_dcode"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
echo "App: fake | Agent: agent | Model: failing-model | Thread: fake"
echo "fake dcode hard failure" >&2
exit 1
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_retry_fake_dcode(tmp_path: Path) -> Path:
    path = tmp_path / "retry_fake_dcode"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
mkdir -p insights scripts deep_dives tables logs
if [ ! -f .retry_seen ]; then
  touch .retry_seen
  cat > insights/candidate_signals.json <<'JSON'
{
  "candidate_signals": [
    {
      "signal_id": "sig_001",
      "signal_type": "paper_cluster",
      "summary": "A partial signal survived the first DeepAgentsCode run.",
      "why_might_matter": "The retry should synthesize this signal without restarting collection.",
      "supporting_tables": ["papers.csv"],
      "related_entities": ["Paper"],
      "suggested_deep_dive": ["Use existing scripts"],
      "initial_score": {"novelty": 0.7, "magnitude": 0.6, "strategic_relevance": 0.8},
      "status": "needs_deep_dive"
    }
  ]
}
JSON
  cat > insights/insight_candidates.json <<'JSON'
{"insight_candidates":[]}
JSON
  echo "first run failed after partial artifacts" >&2
  exit 1
fi
cat > insights/insight_candidates.json <<'JSON'
{
  "insight_candidates": [
    {
      "insight_id": "ins_retry_001",
      "title": "Retry synthesis converted a partial signal into an insight",
      "thesis": "A synthesis-only retry can finish file artifacts after a remote dcode failure.",
      "why_now": "The first run already produced candidate signals and analysis files.",
      "supporting_signals": ["sig_001"],
      "analysis_artifacts": ["scripts/retry_analysis.py", "deep_dives/sig_001.md"],
      "related_entities": ["Paper:Partial signal"],
      "external_support": [],
      "counterarguments": ["This is a fake dcode retry test."],
      "confidence": 0.5,
      "next_questions": ["Run with real dcode."]
    }
  ]
}
JSON
cat > insights/final_brief.md <<'MD'
# Retry brief
MD
cat > scripts/retry_analysis.py <<'PY'
print("retry")
PY
cat > deep_dives/sig_001.md <<'MD'
# Retry deep dive
MD
echo "retry completed"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_fake_pi(tmp_path: Path) -> Path:
    path = tmp_path / "fake_pi"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '{"type":"session","version":3,"id":"fake-pi","timestamp":"2026-01-01T00:00:00Z","cwd":"%s"}\n' "$PWD"
cd "$DATAELF_WORKSPACE"
mkdir -p insights scripts deep_dives tables raw/web logs
echo '{"type":"agent_start"}'
cat > insights/candidate_signals.json <<'JSON'
{
  "candidate_signals": [
    {
      "signal_id": "sig_pi_001",
      "signal_type": "ecosystem_gap",
      "summary": "Pi runner can produce DataElf candidate signals through workspace artifacts.",
      "why_might_matter": "The signal verifies the Pi orchestration contract without depending on Pi internals.",
      "supporting_tables": ["papers.csv"],
      "related_entities": ["Paper", "Tool"],
      "suggested_deep_dive": ["Check Pi artifact handoff"],
      "initial_score": {"novelty": 0.7, "magnitude": 0.6, "strategic_relevance": 0.8},
      "status": "needs_deep_dive"
    }
  ]
}
JSON
cat > insights/insight_candidates.json <<'JSON'
{
  "insight_candidates": [
    {
      "insight_id": "ins_pi_001",
      "title": "Pi can act as a DataElf explorer while DataElf keeps artifact ownership",
      "thesis": "The Pi CLI runner writes the same structured insight files as the dcode runner, keeping DataElf core stable.",
      "why_now": "DataElf is adding a parallel Pi explorer after benchmark evaluation.",
      "supporting_signals": ["sig_pi_001"],
      "analysis_artifacts": ["scripts/pi_analysis.py", "deep_dives/sig_pi_001.md"],
      "related_entities": ["Runtime:Pi", "Workspace:DataElf"],
      "external_support": [{"source_id": "web_pi_001", "summary": "Fake Pi web evidence placeholder."}],
      "counterarguments": ["The fake runner does not exercise real Pi skills."],
      "confidence": 0.64,
      "next_questions": ["Run with the real Pi CLI and pi-web-access package."]
    }
  ]
}
JSON
cat > insights/final_brief.md <<'MD'
# Pi Insight Discovery Brief

Fake Pi output for tests.
MD
cat > scripts/pi_analysis.py <<'PY'
print("pi analysis artifact")
PY
cat > deep_dives/sig_pi_001.md <<'MD'
# sig_pi_001

Deep dive artifact for fake Pi.
MD
cat > tables/external_findings.csv <<'CSV'
finding_id,source_id,finding_type,summary,supports,challenges,confidence,url,source_raw
finding_pi_001,web_pi_001,web_search,External fake Pi finding,ins_pi_001,,0.5,https://example.com/pi,
CSV
echo '{"type":"agent_end","messages":[]}'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_workspace_domain_pack_and_ai_index_client_fixture(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "workspace")
    pack = DomainRegistry().load_domain_pack("ai_index")
    assert pack["domain"] == "ai_index"
    assert "search_papers" in pack["tools"]

    connector = AIIndexConnector(mode="fixture", fixtures_dir=Path("fixtures/ai_index"), workspace_path=workspace)
    client = AIIndexClient(connector=connector, workspace_path=workspace)
    response = client.search_papers(sub_domains=["AI Agent"], sort_type="heat", page=1, size=5)

    assert response["endpoint"] == AI_INDEX_ENDPOINTS["search_papers"]
    assert (workspace / "raw" / "ai_index").exists()
    assert read_table(workspace, "papers")
    assert read_table(workspace, "paper_author")
    assert read_table(workspace, "paper_institution")
    assert (workspace / "tables" / "paper_yearly_counts.csv").exists()
    assert (workspace / "tables" / "paper_awards.csv").exists()

    client.search_scholars(sub_domains=["AI Agent"], sort_type="heat", page=1, size=5)
    client.search_institutions(sub_domains=["AI Agent"], sort_type="heat", page=1, size=5)
    client.fetch_institution_funding("inst_openagent_lab")

    assert read_table(workspace, "scholars")
    assert read_table(workspace, "scholar_institution")
    assert read_table(workspace, "scholar_venues")
    assert read_table(workspace, "institutions")
    assert read_table(workspace, "funding_summary")[0]["total_funding_value_usd"] == "50000000"
    assert read_table(workspace, "funding")
    assert (workspace / "tables" / "funding_investors.csv").exists()
    assert (workspace / "tables" / "papers.csv").exists()


def test_ai_index_table_builder_handles_openapi_shaped_entities(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "workspace")
    raw_uri = str(workspace / "raw" / "ai_index" / "sample.json")

    update_tables_from_response(
        workspace,
        {
            "endpoint": AI_INDEX_ENDPOINTS["search_papers"],
            "raw_uri": raw_uri,
            "data": {
                "list": [
                    {
                        "id": "paper_1",
                        "title": "Agent Benchmarks",
                        "first_authors": ["Alice Chen"],
                        "corresponding_authors": ["Bob Li"],
                        "institution_id": "inst_1",
                        "institution": "OpenAgent Lab",
                        "conference_name": "NeurIPS",
                        "conference_abbreviation": "NeurIPS",
                        "count_by_year": [{"year": 2026, "cited_by_count": 42}],
                        "conf_award_info": {"conf": "NeurIPS", "year": 2026, "awards": [{"key": "spotlight", "title": "Spotlight"}]},
                    }
                ]
            },
        },
    )
    update_tables_from_response(
        workspace,
        {
            "endpoint": AI_INDEX_ENDPOINTS["search_institutions"],
            "raw_uri": raw_uri,
            "data": {
                "list": [
                    {
                        "id": "inst_1",
                        "name": "OpenAgent Lab",
                        "country_code": "US",
                        "conference_names": ["NeurIPS"],
                        "journal_names": ["Nature Machine Intelligence"],
                        "award_list": [{"conf": "NeurIPS", "year": 2026, "awards": [{"key": "best", "title": "Best Paper"}]}],
                        "index_radar_display": {"academic_impact": 91, "capital_signal": 80, "total_score": 88},
                    }
                ]
            },
        },
    )
    update_tables_from_response(
        workspace,
        {
            "endpoint": AI_INDEX_ENDPOINTS["search_scholars"],
            "raw_uri": raw_uri,
            "data": {
                "list": [
                    {
                        "id": "scholar_1",
                        "display_name": "Alice Chen",
                        "institution_id": "inst_1",
                        "institution": "OpenAgent Lab",
                        "count_by_year": [{"year": 2026, "cited_by_count": 12}],
                        "conference_names": ["NeurIPS"],
                        "award_list": [{"conf": "NeurIPS", "year": 2026, "awards": [{"key": "oral", "title": "Oral"}]}],
                    }
                ]
            },
        },
    )
    update_tables_from_response(
        workspace,
        {
            "endpoint": AI_INDEX_ENDPOINTS["fetch_institution_funding"].format(institution_id="inst_1"),
            "request": {"institution_id": "inst_1"},
            "raw_uri": raw_uri,
            "data": {
                "summary": {"total_funding": {"currency": "USD", "value": 100, "value_usd": 100}, "funding_round_count": 1},
                "funding": {
                    "financials_highlights": {"num_investors": 2, "num_lead_investors": 1},
                    "funding_rounds": [
                        {
                            "id": "round_1",
                            "uuid": "uuid_1",
                            "title": "Seed Round",
                            "announced_on": "2026-01-01",
                            "money_raised": {"currency": "USD", "value": 100, "value_usd": 100},
                            "lead_investors": [{"id": "investor_1", "value": "Example Capital"}],
                        }
                    ],
                    "investors": [
                        {
                            "id": "record_1",
                            "type": "investment",
                            "lead_investor": True,
                            "funding_round": {"id": "round_1", "value": "Seed Round"},
                            "investor": {"id": "investor_1", "value": "Example Capital", "type": "organization"},
                        }
                    ],
                },
                "invested": {"investments": []},
            },
        },
    )

    assert read_table(workspace, "paper_yearly_counts")[0]["cited_by_count"] == "42"
    assert read_table(workspace, "paper_awards")[0]["award_title"] == "Spotlight"
    assert read_table(workspace, "institution_venues")[0]["venue_name"] == "NeurIPS"
    assert read_table(workspace, "institution_awards")[0]["award_title"] == "Best Paper"
    assert read_table(workspace, "scholar_yearly_counts")[0]["year"] == "2026"
    assert read_table(workspace, "scholar_awards")[0]["award_key"] == "oral"
    assert read_table(workspace, "funding_rounds")[0]["funding_id"] == "round_1"
    assert read_table(workspace, "funding_investors")[0]["investor_name"] == "Example Capital"


def test_discovery_workflow_creates_job_workspace_and_review(tmp_path: Path, monkeypatch) -> None:
    fake_dcode = _write_fake_dcode(tmp_path)
    monkeypatch.setenv("DATAELF_DCODE_BINARY", str(fake_dcode))
    config = DataElfConfig(
        workspace_dir=tmp_path / ".dataelf",
        sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
        raw_dir=tmp_path / ".dataelf" / "raw",
        workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        fixtures_dir=Path("fixtures/ai_index"),
        ai_index_mode="fixture",
    )
    job = run_discovery("围绕 Agentic LLMs，基于 AI Index 和联网搜索，发现最近值得关注的 3 个 insight", config)

    workspace = Path(job.workspace_path)
    assert job.status == "completed"
    assert job.trigger_type == "user"
    assert job.insight_candidate_ids
    assert (workspace / "insights" / "candidate_signals.json").exists()
    assert (workspace / "insights" / "insight_candidates.json").exists()
    assert (workspace / "insights" / "final_brief.md").exists()
    assert (workspace / "prompts" / "discovery_prompt.md").exists()
    assert (workspace / "logs" / "dcode_stdout.log").exists()
    assert (workspace / ".deepagents" / "agents" / "breadth-scout" / "AGENTS.md").exists()
    assert (workspace / "workspace_index.json").exists()

    review = json.loads((workspace / "reviews" / "quality_review.json").read_text(encoding="utf-8"))
    assert review["review_status"] in {"pass", "pass_with_warnings"}
    assert not config.sqlite_path.exists()


def test_discovery_workflow_can_use_pi_explorer(tmp_path: Path, monkeypatch) -> None:
    fake_pi = _write_fake_pi(tmp_path)
    monkeypatch.setenv("DATAELF_INSIGHTS_EXPLORER", "pi")
    monkeypatch.setenv("DATAELF_PI_BINARY", str(fake_pi))
    config = DataElfConfig(
        workspace_dir=tmp_path / ".dataelf",
        sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
        raw_dir=tmp_path / ".dataelf" / "raw",
        workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        fixtures_dir=Path("fixtures/ai_index"),
        ai_index_mode="fixture",
        insights_explorer="pi",
        pi_binary=str(fake_pi),
        pi_model="openai/gpt-4o",
    )

    job = run_discovery("围绕 Agentic LLMs，发现 1 个 insight", config)

    workspace = Path(job.workspace_path)
    assert job.status == "completed"
    assert job.insight_candidate_ids == ["ins_pi_001"]
    assert (workspace / "logs" / "pi_stdout.log").exists()
    assert (workspace / "logs" / "pi_stderr.log").exists()
    assert (workspace / "logs" / "pi_events.jsonl").exists()
    assert (workspace / "logs" / "pi_command.json").exists()
    command_log = json.loads((workspace / "logs" / "pi_command.json").read_text(encoding="utf-8"))
    command = command_log["command"]
    assert "--mode" in command
    assert "--approve" in command
    assert "--model" in command
    assert "openai/gpt-4o" in command
    assert command_log["cwd"] == str(Path(__file__).resolve().parents[1])
    assert command_log["workspace_path"] == str(workspace)
    prompt = (workspace / "prompts" / "discovery_prompt.md").read_text(encoding="utf-8")
    assert "Pi Runtime" in prompt
    assert "process working directory may be the DataElf repository root" in prompt
    assert "DeepAgentsCode Subagents" not in prompt


def test_discovery_workflow_can_use_sqlite_when_enabled(tmp_path: Path, monkeypatch) -> None:
    fake_dcode = _write_fake_dcode(tmp_path)
    monkeypatch.setenv("DATAELF_DCODE_BINARY", str(fake_dcode))
    config = DataElfConfig(
        workspace_dir=tmp_path / ".dataelf",
        sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
        raw_dir=tmp_path / ".dataelf" / "raw",
        workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        fixtures_dir=Path("fixtures/ai_index"),
        ai_index_mode="fixture",
        enable_sqlite=True,
    )
    job = run_discovery("围绕 Agentic LLMs，发现 1 个 insight", config)

    store = SQLiteStore(config.sqlite_path)
    store.init_schema()
    assert store.get_discovery_job(job.job_id) is not None
    assert store.get_latest_quality_review(job.job_id) is not None
    store.close()


def test_discovery_workflow_skips_quality_review_when_explore_fails(tmp_path: Path, monkeypatch) -> None:
    fake_dcode = _write_failed_fake_dcode(tmp_path)
    monkeypatch.setenv("DATAELF_DCODE_BINARY", str(fake_dcode))
    config = DataElfConfig(
        workspace_dir=tmp_path / ".dataelf",
        sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
        raw_dir=tmp_path / ".dataelf" / "raw",
        workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        fixtures_dir=Path("fixtures/ai_index"),
        ai_index_mode="fixture",
    )

    job = run_discovery("围绕 Agentic LLMs，发现 1 个 insight", config)

    assert job.status == "failed"
    assert job.error == "dcode_exit_1"
    review = json.loads((Path(job.workspace_path) / "reviews" / "quality_review.json").read_text(encoding="utf-8"))
    assert review["review_status"] == "skipped"


def test_quality_review_detects_missing_candidates(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "workspace")
    (workspace / "insights" / "insight_candidates.json").write_text('{"insight_candidates":[]}\n', encoding="utf-8")
    review = review_workspace("job_test", workspace)
    assert review.review_status == "failed"
    assert review.recommended_revision


def test_deepagents_code_cli_missing_binary_is_clear(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "workspace")
    job = DiscoveryJob(job_id="job_missing_dcode", workspace_path=str(workspace), seed_query="test")
    explorer = DeepAgentsCodeCliInsightsExplorer(dcode_binary=str(tmp_path / "missing_dcode"))

    result = explorer.run(job, DiscoveryContext(workspace_path=str(workspace), domain="ai_index"))

    assert result.status == "failed"
    assert "DeepAgentsCode CLI not found" in (result.error or "")
    assert "DeepAgentsCode CLI not found" in (workspace / "logs" / "dcode_stderr.log").read_text(encoding="utf-8")


def test_pi_cli_missing_binary_is_clear(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "workspace")
    job = DiscoveryJob(job_id="job_missing_pi", workspace_path=str(workspace), seed_query="test")
    explorer = PiCliInsightsExplorer(pi_binary=str(tmp_path / "missing_pi"))

    result = explorer.run(job, DiscoveryContext(workspace_path=str(workspace), domain="ai_index", config={"insights_explorer": "pi"}))

    assert result.status == "failed"
    assert result.error == PI_BINARY_NOT_FOUND
    assert "Pi CLI not found" in (workspace / "logs" / "pi_stderr.log").read_text(encoding="utf-8")


def test_deepagents_code_cli_retries_synthesis_after_partial_failure(tmp_path: Path) -> None:
    fake_dcode = _write_retry_fake_dcode(tmp_path)
    workspace = prepare_workspace(tmp_path / "workspace")
    job = DiscoveryJob(job_id="job_retry_dcode", workspace_path=str(workspace), seed_query="test")
    explorer = DeepAgentsCodeCliInsightsExplorer(dcode_binary=str(fake_dcode))

    result = explorer.run(job, DiscoveryContext(workspace_path=str(workspace), domain="ai_index"))

    assert result.status in {"completed", "incomplete"}
    assert result.error is None
    assert "Initial DeepAgentsCode run exited with code 1" in result.warnings[0]
    assert (workspace / "logs" / "dcode_synthesis_retry_stdout.log").exists()
    data = json.loads((workspace / "insights" / "insight_candidates.json").read_text(encoding="utf-8"))
    assert data["insight_candidates"][0]["insight_id"] == "ins_retry_001"


def test_cli_discover_smoke(tmp_path: Path, monkeypatch) -> None:
    fake_dcode = _write_fake_dcode(tmp_path)
    fixtures = tmp_path / "fixtures" / "ai_index"
    fixtures.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "fixtures" / "ai_index"
    for name in ["institutions.json", "papers.json", "scholars.json", "schema_graph.yaml"]:
        (fixtures / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("DATAELF_WORKSPACE", str(tmp_path / ".dataelf"))
    monkeypatch.setenv("DATAELF_FIXTURES_DIR", str(fixtures))
    monkeypatch.setenv("DATAELF_AI_INDEX_MODE", "fixture")
    monkeypatch.setenv("DATAELF_INSIGHTS_EXPLORER", "deepagentscode")
    monkeypatch.setenv("DATAELF_DCODE_BINARY", str(fake_dcode))

    result = CliRunner().invoke(
        app,
        ["discover", "围绕 Agentic LLMs，基于 AI Index 和联网搜索，发现最近值得关注的 3 个 insight"],
    )

    assert result.exit_code == 0
    assert "Discovery job completed" in result.output
    assert "Explorer: deepagentscode" in result.output
    assert "Actual dcode model: fake-model" in result.output
    assert (tmp_path / ".dataelf" / "workspaces").exists()


def test_ai_index_api_defaults_match_provided_curl() -> None:
    assert DEFAULT_AI_INDEX_MODE == "api"
    assert DEFAULT_AI_INDEX_BASE_URL == "https://index.shlab.org.cn/api/v2"
    assert DEFAULT_AI_INDEX_API_KEY == "ak_0XWHy2OQpSKnaKHL"
    assert AI_INDEX_ENDPOINTS["fetch_institution_funding"] == "/openapi/institutions/{institution_id}/funding-profile"


def test_insights_explorer_defaults_and_aliases() -> None:
    assert DEFAULT_INSIGHTS_EXPLORER == "deepagentscode"
    assert normalize_insights_explorer_name("dcode") == "deepagentscode"
    assert normalize_insights_explorer_name("deepagents-code") == "deepagentscode"
    assert normalize_insights_explorer_name("pi") == "pi"


def test_dataelf_config_file_loads_and_env_overrides(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "dataelf.yaml"
    config_path.write_text(
        "\n".join(
            [
                "workspace_dir: file_workspace",
                "fixtures_dir: file_fixtures",
                "model: file-model",
                "ai_index_mode: fixture",
                "enable_sqlite: true",
                "insights_explorer: pi",
                "dcode_binary: file-dcode",
                "dcode_extra_args: --max-turns 7",
                "pi_binary: file-pi",
                "pi_model: file-pi-model",
                "pi_cwd: file-pi-cwd",
                "pi_timeout_seconds: 123",
                "pi_extra_args: --skill /tmp/web-search-skill",
                "pi_stream_logs: true",
                "pi_log_mode: raw",
                "env:",
                "  OPENAI_API_KEY: file-openai",
                "  TAVILY_API_KEY: file-tavily",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAELF_MODEL", "env-model")
    monkeypatch.setenv("DATAELF_PI_MODEL", "env-pi-model")
    monkeypatch.setenv("DATAELF_PI_CWD", "env-pi-cwd")
    monkeypatch.setenv("DATAELF_PI_STREAM_LOGS", "false")
    monkeypatch.setenv("DATAELF_PI_LOG_MODE", "summary")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai")

    config = DataElfConfig.from_env()

    assert config.workspace_dir == Path("file_workspace")
    assert config.fixtures_dir == Path("file_fixtures")
    assert config.model == "env-model"
    assert config.ai_index_mode == "fixture"
    assert config.enable_sqlite
    assert config.insights_explorer == "pi"
    assert config.dcode_binary == "file-dcode"
    assert config.dcode_extra_args == "--max-turns 7"
    assert config.pi_binary == "file-pi"
    assert config.pi_model == "env-pi-model"
    assert config.pi_cwd == Path("env-pi-cwd")
    assert config.pi_timeout_seconds == 123
    assert config.pi_extra_args == "--skill /tmp/web-search-skill"
    assert config.pi_stream_logs is False
    assert config.pi_log_mode == "summary"
    assert config.runtime_env["OPENAI_API_KEY"] == "env-openai"
    assert config.runtime_env["TAVILY_API_KEY"] == "file-tavily"


def test_pi_event_summary_compacts_noisy_json_payload() -> None:
    event = {
        "role": "assistant",
        "content": [
            {
                "type": "toolCall",
                "name": "bash",
                "arguments": {
                    "command": "python - <<'PY'\nprint('very long payload')\nPY" + "x" * 500,
                },
            }
        ],
        "usage": {"input": 12, "output": 3, "totalTokens": 15},
    }

    summary = _summarize_pi_event(json.dumps(event))

    assert summary is not None
    assert "assistant: tool call: bash" in summary
    assert "tokens in=12 out=3 total=15" in summary
    assert len(summary) < 360


def test_pi_event_summary_suppresses_streaming_deltas() -> None:
    assert _summarize_pi_event('{"type":"message_update","content":[{"type":"text","text":"noisy"}]}') is None
    assert _summarize_pi_event('{"type":"message_end","content":[{"type":"text","text":"large final payload"}]}') is None
    assert _summarize_pi_event('{"type":"turn_start"}') is None
    assert _summarize_pi_event('{"type":"turn_end"}') is None
    assert _summarize_pi_event('{"type":"tool_execution_start"}') is None
    assert _summarize_pi_event('{"type":"tool_execution_update","toolName":"bash","content":"partial output"}') is None
    assert _summarize_pi_event('{"type":"tool_execution_end"}') is None


def test_dataelf_init_config_template_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / ".dataelf" / "config.yaml"
    first = write_config_template(path, DataElfConfig(model="first-model"))
    second = write_config_template(path, DataElfConfig(model="second-model"))

    assert first == second
    assert "first-model" in path.read_text(encoding="utf-8")
    assert "second-model" not in path.read_text(encoding="utf-8")


def test_dcode_shell_allow_list_defaults_to_all() -> None:
    assert DEFAULT_SHELL_ALLOW_LIST == "all"
    assert DeepAgentsCodeCliInsightsExplorer().shell_allow_list == "all"


def test_dcode_extra_args_are_appended_and_agents_are_not_overwritten(tmp_path: Path) -> None:
    explorer = DeepAgentsCodeCliInsightsExplorer(extra_args='--max-turns 2 --no-mcp')
    command = explorer._build_command("hello", "openai:gpt-5.5")
    assert command[-5:] == ["--max-turns", "2", "--no-mcp", "-n", "hello"]
    assert "-M" in command
    assert "--model" not in command
    assert "--model-params" in command
    assert '{"use_responses_api": false}' in command
    assert "--max-turns" in command
    assert "--no-mcp" in command
    assert DEFAULT_DCODE_EXTRA_ARGS == ""

    custom_params = DeepAgentsCodeCliInsightsExplorer(extra_args='--model-params {"temperature":0}')._build_command(
        "hello",
        "openai:gpt-5.5",
    )
    assert custom_params.count("--model-params") == 1

    workspace = tmp_path / "workspace"
    custom_agent = workspace / ".deepagents" / "agents" / "breadth-scout" / "AGENTS.md"
    custom_agent.parent.mkdir(parents=True)
    custom_agent.write_text("custom", encoding="utf-8")
    explorer._init_project_agents(workspace)
    assert custom_agent.read_text(encoding="utf-8") == "custom"
