"""Regression contracts for PR #16 domain ownership and component handoff."""

import ast
import json
from pathlib import Path
import pytest
from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import (
    JobSpec,
    StageResult,
    DiscoveryJob,
    DiscoveryContext,
    DomainManifest,
)
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.flows import execution_flow, resolve_spec
from dataelf.discovery.policies import capabilities, register_policy, Capabilities
from dataelf.discovery.domain_services import bind, evidence, services
from dataelf.ontology.contracts import OntologyRequest, OntologyRunResult

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "policy", ["cli_skill_review_v1", "ai_index_cli_skill_review_v1"]
)
def test_declared_alias_has_same_executor_and_scope(policy):
    spec = JobSpec(domain="ai_index", objective="test", workflow_policy=policy)
    assert execution_flow(spec).__module__ == "dataelf.domains.ai_index.workflow"
    assert capabilities(spec).analysis_review
    assert capabilities(spec).unbounded
    for domain, profile in [
        ("finance", "research"),
        ("trajectory_analysis", "research"),
        ("ai_index", "server"),
    ]:
        with pytest.raises(ValueError):
            JobSpec(
                domain=domain,
                objective="test",
                workflow_policy=policy,
                workflow_profile=profile,
            )


@pytest.mark.parametrize("domain", ["ai_index", "finance", "trajectory_analysis"])
def test_historical_selection_stays_legacy(domain):
    saved = JobSpec.from_saved({"domain": domain, "objective": "original"})
    assert saved.workflow_policy == "legacy"
    result = resolve_spec(
        saved, DataElfConfig(workflow_policy="cli_skill_review_v1"), saved
    )
    assert result.workflow_policy == "legacy"
    assert not capabilities(result).analysis_review


def test_manifest_can_register_another_domain_without_core_edit(tmp_path):
    domain = "pr16_fixture"
    folder = tmp_path / domain
    folder.mkdir()
    (folder / "domain.yaml").write_text(
        "domain: pr16_fixture\nversion: '1'\ndisplay_name: Fixture\nplugin: tests.test_pr16_boundaries:unused\nworkflows:\n  pr16_fixture_policy:\n    profiles: [research]\n    entry: tests.test_pr16_boundaries:unused\n    capabilities: {analysis_review: true, unbounded: true}\n"
    )
    DomainRegistry(tmp_path).load_manifest(domain)
    spec = JobSpec(
        domain=domain, objective="fixture", workflow_policy="pr16_fixture_policy"
    )
    assert execution_flow(spec)("marker") == ("marker",)
    assert capabilities(spec).analysis_review
    request = OntologyRequest(
        domain=domain, workspace=tmp_path, workflow_policy=spec.workflow_policy
    )
    assert request.workflow_policy == spec.workflow_policy


def unused(*args):
    return args


def test_generic_contracts_have_no_graph_fields():
    assert "graph_ref" not in StageResult.model_fields
    assert "graph_refs" not in DiscoveryJob.model_fields
    assert not {"graph_ref", "graph_root"} & DiscoveryContext.model_fields.keys()
    assert not any(
        name.startswith(("stage1_", "stage2_"))
        for name in OntologyRunResult.model_fields
    )


def test_source_services_require_explicit_workspace_identity(tmp_path):
    with pytest.raises(ValueError, match="identity"):
        evidence(tmp_path)
    bind(tmp_path, "ai_index")
    catalog = evidence(tmp_path)
    assert catalog.workspace == tmp_path
    with pytest.raises(ValueError, match="cannot change"):
        bind(tmp_path, "finance")
    assert services({"spec": {"domain": "ai_index"}}).source_schemas("reviewer")


def test_shared_components_do_not_import_domains():
    for folder in ["discovery", "ontology"]:
        for path in (ROOT / "dataelf" / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("dataelf.domains."), str(
                        path
                    )
                if isinstance(node, ast.Import):
                    assert not any(
                        item.name.startswith("dataelf.domains.") for item in node.names
                    ), str(path)
    runner = (ROOT / "dataelf/discovery/pi_cli_explorer.py").read_text()
    assert "dataelf.ontology" not in runner
    assert "ai_index" not in runner


def test_shared_extensions_do_not_select_domains():
    for name in [
        "discovery/runtime/analysis.ts",
        "discovery/runtime/compat.ts",
        "discovery/runtime/outputs.ts",
        "ontology/runtime/graph.ts",
        "ontology/runtime/inputs.ts",
        "ontology/runtime/ontology.ts",
    ]:
        body = (ROOT / "dataelf" / name).read_text()
        assert "ai_index" not in body, name
        assert "DATAELF_DOMAIN" not in body, name


def test_domain_legacy_entry_and_resource_isolation():
    registry = DomainRegistry()
    config = DataElfConfig()
    for domain in ["finance", "trajectory_analysis"]:
        assert (
            registry.legacy_entry(domain).__module__
            == f"dataelf.domains.{domain}.entry"
        )
    plugin = registry.load_plugin("ai_index", config)
    legacy = JobSpec(domain="ai_index", objective="test")
    assert not plugin.agent_resources(legacy, config).skills
    new = legacy.model_copy(update={"workflow_policy": "cli_skill_review_v1"})
    assert plugin.agent_resources(new, config).skills


def test_role_services_are_owned_by_selected_domain():
    adapter = services({"domain": "ai_index"})
    for role in [
        "ontology_author",
        "ontology_reviewer",
        "analysis_author",
        "analysis_reviewer",
    ]:
        system, task = adapter.compose(
            role, {"spec": {"domain": "ai_index", "objective": "ORIGINAL TASK"}}, []
        )
        assert "ORIGINAL TASK" in task
        assert "数据源 Skill" in system


@pytest.mark.parametrize(
    "objective, expected",
    [
        ("跨来源核实同一项目的身份", None),
        ("比较3个模型，形成研究报告", None),
        ("最多两条洞察", 2),
        ("至少两条洞察", None),
        ("输出3条发现", 3),
    ],
)
def test_entity_description_is_not_a_report_count(objective, expected):
    from dataelf.domains.ai_index.plugin import _extract_expected_outputs

    assert _extract_expected_outputs(objective) == expected


@pytest.mark.parametrize(
    "policy", ["cli_skill_review_v1", "ai_index_cli_skill_review_v1"]
)
def test_cli_alias_uses_the_same_modeling_override(tmp_path, monkeypatch, policy):
    from typer.testing import CliRunner
    import dataelf.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "_config", lambda: DataElfConfig())

    def run(spec, config):
        seen.update(
            policy=spec.workflow_policy,
            enabled=config.ontology.enabled,
            path=config.ontology.ontology_config,
        )
        return DiscoveryJob(
            job_id="cli_boundary",
            spec=spec,
            status="completed",
            workspace_path=str(tmp_path),
        )

    monkeypatch.setattr(cli, "run_job", run)
    path = tmp_path / "ontology.yaml"
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--domain",
            "ai_index",
            "task",
            "--workflow-policy",
            policy,
            "--modeling",
            "--ontology-config",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == {"policy": policy, "enabled": True, "path": path}


def test_moved_config_adapter_retains_previous_local_override(tmp_path, monkeypatch):
    from dataelf.domains.ai_index import legacy_modeling as adapter

    retired = tmp_path / "removed.yaml"
    previous = tmp_path / "previous.local.yaml"
    current = tmp_path / "current.local.yaml"
    previous.write_text("mode: dynamic\n")
    monkeypatch.setattr(adapter, "LEGACY_DEFAULT", retired)
    monkeypatch.setattr(adapter, "LEGACY_LOCAL", current)
    monkeypatch.setattr(adapter, "PREVIOUS_LEGACY_LOCAL", previous)
    with pytest.warns(DeprecationWarning):
        assert adapter.resolve_config_path(retired) == previous
    current.write_text("mode: template\n")
    with pytest.warns(DeprecationWarning):
        assert adapter.resolve_config_path(retired) == current
