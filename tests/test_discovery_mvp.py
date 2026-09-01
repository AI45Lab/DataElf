from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dataelf.cli import app
from dataelf.config import DataElfConfig, ExplorerConfig, PiConfig, RuntimeConfig, write_config_template
from dataelf.discovery.artifacts import ArtifactContractError, validate_outputs
from dataelf.discovery.contracts import (
    ArtifactRef,
    DiscoveryContext,
    DiscoveryJob,
    DomainManifest,
    JobSpec,
    ModelingStageResult,
    OutputArtifactSpec,
    OutputContract,
    ReviewResult,
    StageResult,
)
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.pi_cli_explorer import _summarize_pi_event
from dataelf.discovery.workflow import run_discovery, run_job
from dataelf.discovery.workspace import prepare_workspace
from dataelf.domains.ai_index.client import AIIndexClient
from dataelf.domains.ai_index.config import (
    AIIndexDomainConfig,
    AIIndexSourceConfig,
    DEFAULT_AI_INDEX_API_KEY,
    DEFAULT_AI_INDEX_BASE_URL,
    DEFAULT_AI_INDEX_MODE,
)
from dataelf.domains.ai_index.connector import AIIndexConnector, AI_INDEX_ENDPOINTS
from dataelf.domains.ai_index.table_builder import read_table
from dataelf.schemas import new_id
from dataelf.stores.sqlite_store import SQLiteStore


def _write_fake_pi(tmp_path: Path) -> Path:
    path = tmp_path / "fake_pi"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '{"type":"session","version":3,"id":"fake-pi","cwd":"%s"}\n' "$PWD"
cd "$DATAELF_WORKSPACE"
if [ "$DATAELF_DOMAIN" = "fake" ]; then
  mkdir -p fake
  printf '{"items":[{"id":"fake_001"}]}\n' > fake/result.json
else
  mkdir -p insights scripts deep_dives tables raw/web
  cat > insights/candidate_signals.json <<'JSON'
{"candidate_signals":[{"signal_id":"sig_pi_001","signal_type":"ecosystem_gap","summary":"Artifact ownership is explicit.","why_might_matter":"The runtime is domain-neutral.","supporting_tables":["papers.csv"],"related_entities":["Paper","Institution"],"suggested_deep_dive":["Validate boundaries"],"initial_score":{"novelty":0.7},"status":"needs_deep_dive"}]}
JSON
  cat > insights/insight_candidates.json <<'JSON'
{"insight_candidates":[{"insight_id":"ins_pi_001","title":"Domain ownership is explicit","thesis":"AI Index behavior is supplied by a plugin while the core validates artifacts.","why_now":"The multi-domain refactor removes core coupling.","supporting_signals":["sig_pi_001"],"analysis_artifacts":["scripts/pi_analysis.py","deep_dives/sig_pi_001.md"],"related_entities":["Paper:x","Institution:y"],"external_support":[{"source_id":"web_1","summary":"test"}],"counterarguments":["This is a fixture run."],"confidence":0.7,"next_questions":["Run live."]}]}
JSON
  printf '# AI Index brief\n' > insights/final_brief.md
  printf 'print("analysis")\n' > scripts/pi_analysis.py
  printf '# Deep dive\n' > deep_dives/sig_pi_001.md
  printf 'finding_id,summary\nf1,test\n' > tables/external_findings.csv
fi
echo '{"type":"agent_end","messages":[]}'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _config(tmp_path: Path, pi_binary: Path, *, sqlite: bool = False) -> DataElfConfig:
    runtime = RuntimeConfig(
        workspace_dir=tmp_path / ".dataelf",
        sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
        workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        enable_sqlite=sqlite,
    )
    ai = AIIndexDomainConfig(source=AIIndexSourceConfig(mode="fixture", fixtures_dir=Path("fixtures/ai_index")))
    return DataElfConfig(
        runtime=runtime,
        explorer=ExplorerConfig(pi=PiConfig(binary=str(pi_binary), model="openai/gpt-test")),
        domains={"ai_index": ai.model_dump(mode="python")},
    )


class FakeModeler:
    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult:
        workspace = Path(context.workspace_path)
        path = workspace / "fake" / "evidence.txt"
        path.write_text("modeled\n", encoding="utf-8")
        return ModelingStageResult(
            status="completed",
            artifacts=[ArtifactRef(
                artifact_id="fake_evidence", kind="fake_model", path="fake/evidence.txt",
                role="evidence", producer_stage="domain_modeling", media_type="text/plain",
            )],
        )


class FailingModeler:
    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult:
        return ModelingStageResult(
            status="failed", error_code="FAKE_MODEL_FAILED", error_message="expected test failure",
        )


class FakePlugin:
    def __init__(self, manifest: DomainManifest):
        self.manifest = manifest

    def normalize_spec(self, spec: JobSpec) -> JobSpec:
        return spec

    def prepare(self, spec: JobSpec, workspace_path: str, config: Any) -> StageResult:
        (Path(workspace_path) / "fake").mkdir(parents=True, exist_ok=True)
        return StageResult(status="completed", context={"prepared": True}, env={"FAKE_READY": "1"})

    def create_modeler(self, spec: JobSpec, config: Any) -> FakeModeler | None:
        if spec.modeling_strategy == "fail_model":
            return FailingModeler()
        return FakeModeler() if spec.modeling_strategy == "fake_model" else None

    def build_prompt(self, job: DiscoveryJob, context: DiscoveryContext) -> str:
        return "Analyze the fake input and create the declared result."

    def output_contract(self, spec: JobSpec) -> OutputContract:
        return OutputContract(
            contract_id="fake.result",
            artifacts=[OutputArtifactSpec(
                artifact_id="fake_result", path="fake/result.json", kind="result",
                media_type="application/json", json_root="items",
            )],
        )

    def review(self, job: DiscoveryJob, workspace_path: str) -> ReviewResult:
        return ReviewResult(review_id=new_id("review"), job_id=job.job_id, status="pass")

    def result_ids(self, workspace_path: str) -> list[str]:
        return ["fake_001"]


def create_fake_plugin(config: DataElfConfig, manifest: DomainManifest) -> FakePlugin:
    return FakePlugin(manifest)


def _fake_registry(tmp_path: Path) -> DomainRegistry:
    root = tmp_path / "domains"
    domain = root / "fake"
    domain.mkdir(parents=True)
    (domain / "domain.yaml").write_text(
        "\n".join([
            "domain: fake", "version: '1'", "display_name: Fake",
            f"plugin: {__name__}:create_fake_plugin", "capabilities: [modeling]", "workspace_dirs: [fake]", "",
        ]),
        encoding="utf-8",
    )
    return DomainRegistry(root)


def test_core_workspace_is_domain_neutral(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "workspace", JobSpec(domain="fake", objective="test"))
    assert (workspace / "raw").is_dir()
    assert (workspace / "artifacts").is_dir()
    assert not (workspace / "raw" / "ai_index").exists()
    assert not (workspace / "insights" / "insight_candidates.json").exists()
    assert json.loads((workspace / "job_spec.json").read_text(encoding="utf-8"))["domain"] == "fake"


def test_job_and_manifest_reject_path_traversal() -> None:
    with pytest.raises(ValueError):
        JobSpec(domain="../fake", objective="test")
    with pytest.raises(ValueError):
        DomainManifest(
            domain="fake", version="1", display_name="Fake", plugin="x:y",
            workspace_dirs=["../escape"],
        )


def test_registry_loads_typed_manifest_and_plugin() -> None:
    config = DataElfConfig()
    manifest = DomainRegistry().load_manifest("ai_index")
    plugin = DomainRegistry().load_plugin("ai_index", config)
    assert manifest.domain == "ai_index"
    assert manifest.plugin == "dataelf.domains.ai_index.plugin:create_plugin"
    assert plugin.manifest == manifest


def test_discovery_core_has_no_domain_imports() -> None:
    discovery = Path(__file__).resolve().parents[1] / "dataelf" / "discovery"
    offenders = [
        path.name for path in discovery.glob("*.py")
        if "dataelf.domains" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_fake_domain_runs_without_core_changes(tmp_path: Path) -> None:
    pi = _write_fake_pi(tmp_path)
    config = DataElfConfig(
        runtime=RuntimeConfig(
            workspace_dir=tmp_path / ".dataelf",
            sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
            workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        ),
        explorer=ExplorerConfig(pi=PiConfig(binary=str(pi))),
    )
    job = run_job(
        JobSpec(domain="fake", objective="test fake domain", modeling_strategy="fake_model"),
        config,
        registry=_fake_registry(tmp_path),
    )
    workspace = Path(job.workspace_path)
    assert job.status == "completed"
    assert {item.artifact_id for item in job.artifacts} >= {"fake_evidence", "fake_result", "pi_events"}
    assert json.loads((workspace / "workspace_index.json").read_text(encoding="utf-8"))["result_ids"] == ["fake_001"]
    prompt = (workspace / "prompts" / "discovery_prompt.md").read_text(encoding="utf-8")
    assert "fake/evidence.txt" in prompt
    assert "fake/result.json" in prompt


def test_output_contract_rejects_workspace_escape(tmp_path: Path) -> None:
    contract = OutputContract(
        contract_id="unsafe",
        artifacts=[OutputArtifactSpec(artifact_id="x", path="../x.json", kind="x")],
    )
    with pytest.raises(ArtifactContractError, match="escapes workspace"):
        validate_outputs(tmp_path, contract)


def test_modeling_failure_is_attributed_to_domain_modeling(tmp_path: Path) -> None:
    pi = _write_fake_pi(tmp_path)
    config = DataElfConfig(
        runtime=RuntimeConfig(
            workspace_dir=tmp_path / ".dataelf",
            sqlite_path=tmp_path / ".dataelf" / "dataelf.sqlite",
            workspaces_dir=tmp_path / ".dataelf" / "workspaces",
        ),
        explorer=ExplorerConfig(pi=PiConfig(binary=str(pi))),
    )
    job = run_job(
        JobSpec(domain="fake", objective="fail", modeling_strategy="fail_model"),
        config,
        registry=_fake_registry(tmp_path),
    )
    review = json.loads((Path(job.workspace_path) / "reviews" / "quality_review.json").read_text(encoding="utf-8"))
    assert job.status == "failed"
    assert job.error_code == "FAKE_MODEL_FAILED"
    assert review["metrics"]["failed_stage"] == "domain_modeling"
    assert not (Path(job.workspace_path) / "logs" / "pi_command.json").exists()


def test_ai_index_workflow_keeps_discover_cli_behavior(tmp_path: Path) -> None:
    config = _config(tmp_path, _write_fake_pi(tmp_path))
    job = run_discovery("围绕 Agentic LLMs，发现 1 个 insight", config)
    workspace = Path(job.workspace_path)
    assert job.status == "completed"
    assert job.spec.domain == "ai_index"
    assert job.spec.parameters["expected_outputs"] == 1
    assert (workspace / "raw" / "ai_index").is_dir()
    assert (workspace / "tables" / "papers.csv").is_file()
    assert (workspace / "insights" / "insight_candidates.json").is_file()
    assert json.loads((workspace / "workspace_index.json").read_text(encoding="utf-8"))["result_ids"] == ["ins_pi_001"]
    assert {item.artifact_id for item in job.artifacts} >= {"candidate_signals", "insight_candidates", "final_brief"}


def test_missing_pi_fails_before_output_validation(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "missing_pi")
    job = run_discovery("test", config)
    review = json.loads((Path(job.workspace_path) / "reviews" / "quality_review.json").read_text(encoding="utf-8"))
    assert job.status == "failed"
    assert job.error_code == "PI_BINARY_NOT_FOUND"
    assert review["status"] == "skipped"
    assert review["metrics"]["failed_stage"] == "explorer"


def test_sqlite_stores_new_job_and_review_contract(tmp_path: Path) -> None:
    config = _config(tmp_path, _write_fake_pi(tmp_path), sqlite=True)
    job = run_discovery("test", config)
    store = SQLiteStore(config.runtime.sqlite_path)
    store.init_schema()
    assert store.get_discovery_job(job.job_id) == job
    review = store.get_latest_quality_review(job.job_id)
    assert review is not None and review.status in {"pass", "pass_with_warnings"}
    store.close()


def test_ai_index_client_fixture_still_works(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connector = AIIndexConnector(mode="fixture", fixtures_dir=Path("fixtures/ai_index"), workspace_path=workspace)
    client = AIIndexClient(connector=connector, workspace_path=workspace)
    response = client.search_papers(sub_domains=["AI Agent"], sort_type="heat", page=1, size=5)
    assert response["endpoint"] == AI_INDEX_ENDPOINTS["search_papers"]
    assert read_table(workspace, "papers")
    assert read_table(workspace, "paper_author")
    client.search_scholars(page=1, size=5)
    client.search_institutions(page=1, size=5)
    client.fetch_institution_funding("inst_openagent_lab")
    assert read_table(workspace, "funding_summary")[0]["total_funding_value_usd"] == "50000000"


def test_nested_config_file_and_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "dataelf.yaml"
    path.write_text(
        """runtime:
  workspace_dir: file-workspace
  enable_sqlite: true
explorer:
  type: pi
  pi:
    binary: file-pi
    model: file-model
domains:
  ai_index:
    source:
      mode: fixture
      fixtures_dir: file-fixtures
env:
  OPENAI_API_KEY: file-key
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAELF_PI_MODEL", "env-model")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    config = DataElfConfig.from_env()
    ai = AIIndexDomainConfig.from_mapping(config.domain_config("ai_index"))
    assert config.runtime.workspace_dir == Path("file-workspace")
    assert config.runtime.enable_sqlite
    assert config.explorer.pi.binary == "file-pi"
    assert config.explorer.pi.model == "env-model"
    assert ai.source.mode == "fixture"
    assert ai.source.fixtures_dir == Path("file-fixtures")
    assert config.env["OPENAI_API_KEY"] == "env-key"


def test_config_template_uses_only_nested_schema(tmp_path: Path) -> None:
    path = write_config_template(tmp_path / "dataelf.local.yaml", DataElfConfig())
    text = path.read_text(encoding="utf-8")
    assert "runtime:" in text and "explorer:" in text and "domains:" in text
    assert "insights_explorer:" not in text
    assert "ai_index_modeling:" not in text
    assert "dcode" not in text.lower()


def test_flat_legacy_config_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "dataelf.yaml").write_text(
        "workspace_dir: .dataelf\ninsights_explorer: pi\nai_index_mode: fixture\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Unknown DataElf config keys"):
        DataElfConfig.from_env()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("domains:\n  ai_index: true\n", "domains.ai_index must be a mapping/object"),
        ("runtime:\n  enable_sqlite: ture\n", "Expected a boolean value"),
        ("explorer:\n  pi:\n    timeout_seconds: -1\n", "greater than or equal to 1"),
    ],
)
def test_malformed_nested_config_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    message: str,
) -> None:
    (tmp_path / "dataelf.yaml").write_text(payload, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=message):
        DataElfConfig.from_env()


def test_disabled_ai_index_modeling_preserves_dormant_settings() -> None:
    domain = AIIndexDomainConfig.from_mapping({
        "modeling": {
            "enabled": False,
            "ontology_template": "ai_index_search",
            "stage1_config": "not-loaded-while-disabled/stage1.yaml",
            "stage2_config": "not-loaded-while-disabled/stage2.yaml",
            "model_name": "deepseek-v4-flash",
        },
    })

    assert domain.modeling.enabled is False
    assert domain.modeling.ontology_template == "ai_index_search"
    assert domain.modeling.model_name == "deepseek-v4-flash"
    domain.validate_for_run()


def test_ai_index_active_config_is_preflighted() -> None:
    with pytest.raises(ValueError, match="Input should be 'api' or 'fixture'"):
        AIIndexDomainConfig.from_mapping({"source": {"mode": "fixtures"}})

    empty_api = AIIndexDomainConfig.from_mapping({
        "source": {"mode": "api", "base_url": "", "api_key": ""},
    })
    with pytest.raises(ValueError, match="base_url"):
        empty_api.validate_for_run()

    missing_stages = AIIndexDomainConfig.from_mapping({
        "modeling": {
            "enabled": True,
            "stage1_config": "missing/stage1.yaml",
            "stage2_config": "missing/stage2.yaml",
        },
    })
    with pytest.raises(ValueError, match="stage1_config is not a file"):
        missing_stages.validate_for_run()

    unknown_template = AIIndexDomainConfig.from_mapping({
        "modeling": {"enabled": True, "ontology_template": "does_not_exist"},
    })
    with pytest.raises(ValueError, match="unknown ontology template"):
        unknown_template.validate_for_run()


def test_optional_modeling_strings_are_trimmed() -> None:
    domain = AIIndexDomainConfig.from_mapping({
        "modeling": {"enabled": False, "ontology_template": "   ", "model_name": " model-name  "},
    })
    assert domain.modeling.ontology_template is None
    assert domain.modeling.model_name == "model-name"


def test_cli_discover_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pi = _write_fake_pi(tmp_path)
    (tmp_path / "dataelf.local.yaml").write_text(
        """
domains:
  ai_index:
    modeling:
      enabled: false
      ontology_template: ai_index_search
      stage1_config: not-loaded-while-disabled/stage1.yaml
      stage2_config: not-loaded-while-disabled/stage2.yaml
      model_name: deepseek-v4-flash
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAELF_WORKSPACE", str(tmp_path / ".dataelf"))
    monkeypatch.setenv("DATAELF_PI_BINARY", str(pi))
    monkeypatch.setenv("DATAELF_AI_INDEX_MODE", "fixture")
    monkeypatch.setenv("DATAELF_FIXTURES_DIR", str(Path(__file__).resolve().parents[1] / "fixtures" / "ai_index"))
    result = CliRunner().invoke(app, ["discover", "围绕 Agentic LLMs，发现 1 个 insight"])
    assert result.exit_code == 0
    assert "Discovery job completed" in result.output
    assert "Explorer: pi" in result.output


def test_cli_rejects_explicit_template_when_modeling_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "dataelf.local.yaml").write_text(
        "domains:\n  ai_index:\n    modeling:\n      enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        ["discover", "test", "--ontology-template", "ai_index_search"],
    )
    assert result.exit_code == 2
    assert "requires --ai-index-modeling" in result.output


def test_ai_index_defaults_and_pi_event_summary() -> None:
    assert DEFAULT_AI_INDEX_MODE == "api"
    assert DEFAULT_AI_INDEX_BASE_URL == "https://index.shlab.org.cn/api/v2"
    assert DEFAULT_AI_INDEX_API_KEY == "ak_0XWHy2OQpSKnaKHL"
    domain = AIIndexDomainConfig.from_mapping({})
    assert domain.modeling.stage1_config.is_file()
    assert domain.modeling.stage2_config.is_file()
    summary = _summarize_pi_event(json.dumps({
        "role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "x" * 500}}],
        "usage": {"input": 12, "output": 3, "totalTokens": 15},
    }))
    assert summary is not None
    assert "assistant: tool call: bash" in summary
    assert "tokens in=12 out=3 total=15" in summary
    assert len(summary) < 360
