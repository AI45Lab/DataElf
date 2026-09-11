from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from dataelf.cli import app
from dataelf.domains.ai_index.config import AIIndexDomainConfig, AIIndexModelingConfig
from dataelf.domains.ai_index.modeling.contracts import OntologyRunResult
from dataelf.domains.ai_index.modeling.ontology.config import DEFAULT_ONTOLOGY_CONFIG, load_config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import load_config as load_stage1
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import load_config as load_stage2
from dataelf.domains.ai_index.modeling.ontology_runner import AIIndexOntologyRunner


def config_payload() -> dict:
    return yaml.safe_load(DEFAULT_ONTOLOGY_CONFIG.read_text())


def write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "ontology.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def test_defaults_share_one_file_and_resolve_moved_paths() -> None:
    config = load_config()
    assert AIIndexModelingConfig().ontology_config == DEFAULT_ONTOLOGY_CONFIG
    assert config.path == config.stage1.path == config.stage2.path == DEFAULT_ONTOLOGY_CONFIG
    assert load_stage1(config.path) == config.stage1
    assert load_stage2(config.path) == config.stage2
    assert config.stage1.ontology.domain_pack_path == DEFAULT_ONTOLOGY_CONFIG.parents[2] / "domain.yaml"
    assert config.stage1.pi.repo == Path(__file__).resolve().parents[1]
    assert set(AIIndexModelingConfig().model_dump()) == {"enabled", "ontology_config"}


def test_runner_inventory_uses_published_rdf_not_external_export(tmp_path, monkeypatch):
    from dataelf.domains.ai_index.modeling import template as templates
    from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1 import pipeline as stage1
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2 import pipeline as stage2
    from dataelf.domains.ai_index.modeling.pipeline import _modeling_artifacts

    workspace = tmp_path / 'job'
    first = workspace / 'ontology/stage1/published/test'
    second = workspace / 'ontology/stage2/published/test'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for name in ['ontology.json', 'grounding.json']:
        (first / name).write_text('{}')
    for name in ['graph.rdf', 'graph.nq', 'graph.nt']:
        (second / name).write_text('published RDF')
    (second / 'manifest.json').write_text('{"status":"completed"}')
    (second / 'validation.json').write_text('{"status":"valid"}')
    stable = tmp_path / 'job.rdf'
    stable.write_text('published RDF')
    payload = config_payload()
    payload['ontology_template'] = 'ai_index_search'
    config = write_config(tmp_path, payload)
    monkeypatch.setattr(templates, 'bind_template', lambda *a: {'status':'completed', 'runId':'test', 'bundle':str(first)})
    monkeypatch.setattr(stage1, 'validate_published_bundle', lambda *a: {'status':'valid'})
    monkeypatch.setattr(stage2, 'build', lambda *a, **kw: {'status':'completed', 'runId':'test', 'bundle':str(second), 'stableRdf':str(stable)})
    monkeypatch.setattr(stage2, 'validate_published', lambda *a: {'status':'valid'})

    result = AIIndexOntologyRunner(config).run(workspace)
    assert result.status == 'completed'
    assert result.rdfxml_path == str(second / 'graph.rdf')
    artifacts = _modeling_artifacts(workspace, result)
    xml = next(ref for ref in artifacts if ref.artifact_id == 'ai_index_rdfxml')
    assert xml.path == 'ontology/stage2/published/test/graph.rdf'
    assert all((workspace / ref.path).is_relative_to(workspace) for ref in artifacts)
    assert stable.read_text() == 'published RDF'


def test_external_config_resolves_paths_relative_to_itself(tmp_path, monkeypatch) -> None:
    payload = config_payload()
    payload["stage1"]["ontology"]["domain_pack_path"] = "domain.yaml"
    payload["stage1"]["pi"].update(repo="runtime", node="bin/node")
    path = write_config(tmp_path, payload)
    monkeypatch.chdir(tmp_path.parent)
    config = load_config(path)
    assert config.stage1.ontology.domain_pack_path == tmp_path / "domain.yaml"
    assert config.stage1.pi.repo == tmp_path / "runtime"
    assert config.stage1.pi.node == tmp_path / "bin/node"


@pytest.mark.parametrize("key,value", [("stage1", None), ("stage2", {}), ("raw_page_size", 51), ("worker_timeout_seconds", 0)])
def test_invalid_unified_config_is_rejected(tmp_path, key, value) -> None:
    payload = config_payload()
    payload[key] = value
    with pytest.raises(ValueError, match=key):
        load_config(write_config(tmp_path, payload))


def test_old_split_schema_and_outer_overrides_are_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown ontology config keys"):
        load_config(write_config(tmp_path, config_payload()["stage1"]))
    with pytest.raises(ValueError, match="stage1_config"):
        AIIndexModelingConfig(stage1_config="old.yaml")
    with pytest.raises(ValueError, match="model_name"):
        AIIndexModelingConfig(model_name="ignored-model")
    payload = config_payload()
    payload["stage2"]["stage1_config"] = "old.yaml"
    with pytest.raises(ValueError, match="stage1_config"):
        load_config(write_config(tmp_path, payload))


def test_preflight_checks_template_and_both_stages(tmp_path) -> None:
    payload = config_payload()
    payload["ontology_template"] = "does_not_exist"
    path = write_config(tmp_path, payload)
    domain = AIIndexDomainConfig.model_validate({
        "source": {"mode": "fixture", "fixtures_dir": str(Path(__file__).resolve().parents[1] / "fixtures/ai_index")},
        "modeling": {"enabled": True, "ontology_config": path},
    })
    with pytest.raises(ValueError, match="unknown ontology template"):
        domain.validate_for_run()
    payload["ontology_template"] = None
    payload["stage2"]["compiler"]["request_timeout_seconds"] = 0
    write_config(tmp_path, payload)
    with pytest.raises(ValueError, match="compiler.request_timeout_seconds"):
        domain.validate_for_run()
    domain.modeling.enabled = False
    path.unlink()
    domain.validate_for_run()


def test_only_enable_and_path_have_outer_environment_overrides(tmp_path, monkeypatch) -> None:
    path = write_config(tmp_path, config_payload())
    monkeypatch.setenv("DATAELF_AI_INDEX_MODELING_ENABLED", "true")
    monkeypatch.setenv("DATAELF_AI_INDEX_MODELING_ONTOLOGY_CONFIG", str(path))
    monkeypatch.setenv("DATAELF_AI_INDEX_MODELING_MODEL_NAME", "must-not-override")
    domain = AIIndexDomainConfig.from_mapping({})
    assert domain.modeling.enabled
    assert domain.modeling.ontology_config == path
    assert load_config(path).stage1.generator.name != "must-not-override"


@pytest.mark.parametrize("template", [None, "ai_index_search"])
def test_runner_passes_unified_parameters_without_overrides(tmp_path, monkeypatch, template) -> None:
    from dataelf.domains.ai_index.modeling import template as templates
    from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1 import pipeline as stage1
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2 import pipeline as stage2

    payload = config_payload()
    payload["ontology_template"] = template
    payload["stage1"]["generator"].update(name="generator-test", max_tokens=1234, request_timeout_seconds=37)
    payload["stage1"]["reviewer"].update(name="reviewer-test", process_timeout_seconds=53)
    payload["stage2"]["compiler"].update(name="compiler-test", max_tokens=2345, request_max_retries=1)
    payload["stage2"]["total_stage_timeout_seconds"] = 83
    path = write_config(tmp_path, payload)
    received = {}

    def generated(**kwargs):
        assert template is None
        assert kwargs["resume"] is None and kwargs["repair_from"] is None
        received["stage1"] = kwargs["config"]
        return {"status": "completed", "runId": "test1", "bundle": str(tmp_path / "stage1")}

    def bound(config, workspace, template_id):
        assert template_id == template == "ai_index_search"
        received["stage1"] = config
        return {"status": "completed", "runId": "test1", "bundle": str(tmp_path / "stage1")}

    def built(config, workspace, *, resume_run_id):
        received["stage2"] = config
        assert resume_run_id is None
        return {"status": "failed", "runId": "test2"}

    monkeypatch.setattr(stage1, "generate_pipeline", generated)
    monkeypatch.setattr(templates, "bind_template", bound)
    monkeypatch.setattr(stage1, "validate_published_bundle", lambda *a: {"status": "valid"})
    monkeypatch.setattr(stage2, "build", built)
    result = AIIndexOntologyRunner(path).run(tmp_path)
    assert result.stage == "stage2"
    assert received["stage1"] == load_config(path).stage1
    assert received["stage2"] == load_config(path).stage2
    assert result.details["stage1Attempts"] == (0 if template else 1)


def test_modeler_reads_collection_and_template_options(tmp_path) -> None:
    from dataelf.domains.ai_index.modeling.pipeline import AIIndexModeler

    payload = config_payload()
    payload.update(raw_page_size=7, ontology_template="  ai_index_search  ")
    domain = AIIndexDomainConfig.model_validate({"modeling": {"enabled": True, "ontology_config": write_config(tmp_path, payload)}})
    modeler = AIIndexModeler(domain, {})
    assert modeler.collector.page_size == 7
    assert modeler.ontology_config.ontology_template == "ai_index_search"


def test_subprocess_uses_config_deadline_and_absolute_path(tmp_path, monkeypatch) -> None:
    from dataelf.domains.ai_index.modeling import subprocess_runner

    payload = config_payload()
    payload["worker_timeout_seconds"] = 97
    write_config(tmp_path, payload)
    monkeypatch.chdir(tmp_path)
    config = AIIndexModelingConfig(enabled=True, ontology_config="ontology.yaml")
    seen = {}

    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            seen["command"] = command
        def communicate(self, *, timeout):
            seen["timeout"] = timeout
            return "", ""

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", Process)
    monkeypatch.setattr(subprocess_runner, "terminate_process", lambda p: None)
    subprocess_runner.run_ontology_subprocess(tmp_path, config, {})
    request = json.loads((tmp_path / "modeling/ai_index/worker_request.json").read_text())
    assert request["modeling"] == {"enabled": True, "ontology_config": str(tmp_path / "ontology.yaml")}
    assert seen["timeout"] == 97


def test_worker_reads_config_path_from_request(tmp_path, monkeypatch) -> None:
    from dataelf.domains.ai_index.modeling import worker

    path = write_config(tmp_path, config_payload())
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"workspace_path": str(tmp_path), "modeling": {"enabled": True, "ontology_config": str(path)}}))
    seen = {}
    class Runner:
        def __init__(self, config_path, *, progress):
            seen["config_path"] = config_path
            progress("stage1")
        def run(self, workspace):
            return OntologyRunResult(status="incomplete", stage="stage1")
    monkeypatch.setattr(worker, "AIIndexOntologyRunner", Runner)
    result = worker.execute(request, tmp_path / "result.json", tmp_path / "progress.json")
    assert result.status == "incomplete"
    assert seen["config_path"] == path


def test_cli_forwards_selected_config(tmp_path, monkeypatch) -> None:
    from dataelf.config import DataElfConfig
    from dataelf.discovery.contracts import DiscoveryJob, JobSpec
    import dataelf.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "_config", lambda: DataElfConfig())
    def run(spec, config):
        assert spec.domain == "ai_index"
        assert spec.parameters == {"expected_outputs": 2}
        assert spec.modeling_strategy == "ontology_rdf"
        seen.update(config.domain_config("ai_index")["modeling"])
        return DiscoveryJob(job_id="test", spec=spec, workspace_path=str(tmp_path), status="completed")
    monkeypatch.setattr(cli, "run_job", run)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--domain", "ai_index", "test", "--modeling", "--ontology-config", "ontology.yaml",
                                    "--param", "expected_outputs=2", "--modeling-strategy", "ontology_rdf"])
    assert result.exit_code == 0, result.output
    assert seen == {"enabled": True, "ontology_config": tmp_path / "ontology.yaml"}


def test_unified_config_builds_fixture_template_and_rdf(tmp_path) -> None:
    from dataclasses import replace
    from dataelf.discovery.contracts import DiscoveryJob, JobSpec
    from dataelf.domains.ai_index.modeling.acquisition import AIIndexRawCollector
    from dataelf.domains.ai_index.modeling.template import bind_template
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.compiler import ENDPOINT_SLUGS, build_seed_plan
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import resolve_stage1_contract
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.rdf import materialize, nquads, ntriples, rdfxml
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.validation import validate_candidate

    config = load_config()
    fixtures = Path(__file__).resolve().parents[1] / "fixtures/ai_index"
    job = DiscoveryJob(job_id="config_fixture", spec=JobSpec(domain="ai_index", objective="ontology config fixture"), workspace_path=str(tmp_path))
    AIIndexRawCollector(mode="fixture", base_url="", api_key="", fixtures_dir=fixtures, page_size=config.raw_page_size).collect(job, tmp_path)
    stage1 = bind_template(replace(config.stage1, quality=replace(config.stage1.quality, manual_audit_required=False)), tmp_path, "ai_index_search")
    assert stage1["status"] == "completed"
    contract = resolve_stage1_contract(config.stage2, tmp_path)
    plans = {endpoint: build_seed_plan(contract, endpoint) for endpoint in ENDPOINT_SLUGS}
    graph = materialize(plans, contract, config.stage2)
    nq, nt, xml = (tmp_path / name for name in ("graph.nq", "graph.nt", "graph.rdf"))
    nq.write_text(nquads(graph))
    nt.write_text(ntriples(graph))
    xml.write_bytes(rdfxml(graph, config.stage1.ontology.namespace, config.stage2.provenance_namespace))
    validation = validate_candidate(graph=graph, contract=contract, config=config.stage2, nq_path=nq, nt_path=nt, rdfxml_path=xml, plan_hashes={})
    assert validation["status"] == "valid", validation.get("errors")
    assert graph.metrics["quadCount"] > 0
