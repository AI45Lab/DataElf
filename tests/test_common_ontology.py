from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import Dataset, Literal, URIRef
from rdflib.namespace import XSD

from dataelf.ontology import OntologyContract, OntologyRequest, SourceInput, run_ontology
from dataelf.ontology.config import OntologyConfig, load_config
from dataelf.ontology.contracts import ExtractionPlan, OntologyTemplate
from dataelf.ontology.engine import materialize, validate_contract
from dataelf.ontology.sources import SourceCatalog, digest, write_json


def retail_contract():
    def ident(kind, path):
        return {"op": "concat", "args": ["urn:retail:", kind, "/", {"path": path}]}
    return OntologyContract.model_validate({
        "domain": "retail", "namespace": "urn:retail:", "classes": {"Customer": {}, "Order": {}, "Tag": {}},
        "properties": {"name": {"domain": "Customer", "range": "xsd:string"},
                       "amount": {"domain": "Order", "range": "xsd:integer"},
                       "buyer": {"kind": "iri", "domain": "Order", "range": "Customer"},
                       "tag": {"kind": "iri", "domain": "Order", "range": "Tag"}},
        "mappings": [
            {"id": "customers", "source": "customers", "evidence": ["customers.csv#/0"],
             "nodes": [{"id": "customer", "class_id": "Customer", "iri": ident("customer", "/id")}],
             "triples": [{"subject": "customer", "predicate": "name", "value": {"path": "/name"}, "conflict": "consensus"}]},
            {"id": "orders", "source": "orders", "evidence": ["orders.json#/0"],
             "nodes": [{"id": "order", "class_id": "Order", "iri": ident("order", "/id")},
                       {"id": "customer", "class_id": "Customer", "iri": ident("customer", "/buyer/id")}],
             "triples": [{"subject": "order", "predicate": "amount", "value": {"path": "/amount"}},
                         {"subject": "order", "predicate": "buyer", "kind": "iri", "value": {"node": "customer"}}],
             "children": [{"for_each": {"path": "/tags"}, "nodes": [{"id": "tag", "class_id": "Tag", "iri": ident("tag", "")}],
                           "triples": [{"subject": "order", "predicate": "tag", "kind": "iri", "value": {"node": "tag"}}]}]},
        ], "constraints": [{"class_id": "Order", "predicate": "buyer", "min_count": 1, "max_count": 1}],
        "queries": [{"id": "zero_is_known", "query": "ASK { ?order <urn:retail:amount> 0 }", "expect": True}],
    })


def retail_request(tmp_path, fmt="json"):
    (tmp_path / "customers.csv").write_text("id,name\nc1,Alice\nc1,Alicia\nc2,Bob\n")
    rows = [{"id": "o1", "buyer": {"id": "c1"}, "amount": 0, "tags": ["a", "b"], "new_field": 8},
            {"id": "o2", "buyer": {"id": "c2"}, "amount": None}]
    path = tmp_path / ("orders." + fmt)
    path.write_text(json.dumps(rows) if fmt == "json" else "\n".join(json.dumps(r) for r in rows))
    return OntologyRequest(domain="retail", workspace=tmp_path, sources=[SourceInput(source_id="customers", path=Path("customers.csv")), SourceInput(source_id="orders", path=path)])


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_non_ai_formats_nested_join_consensus_and_provenance(tmp_path, fmt):
    request = retail_request(tmp_path, fmt)
    catalog = SourceCatalog(tmp_path, request.sources)
    graph, lineage, validation = materialize(retail_contract(), catalog)
    assert validation["status"] == "valid", validation
    assert (URIRef("urn:retail:order/o1"), URIRef("urn:retail:amount"), Literal(0, datatype=XSD.integer)) in graph
    assert not list(graph.objects(URIRef("urn:retail:order/o2"), URIRef("urn:retail:amount")))
    assert not list(graph.objects(URIRef("urn:retail:customer/c1"), URIRef("urn:retail:name")))
    assert len(lineage["conflicts"]) == 1
    assert "/new_field" in lineage["unmappedFields"]["orders"]
    assert len(lineage["records"]) == 5
    for record in lineage["records"]:
        assert (tmp_path / record["file"]).is_file()
        assert record["pointer"].startswith("/")


def test_template_runs_without_agent_and_publishes_hashes(tmp_path):
    request = retail_request(tmp_path)
    result = run_ontology(request, config=OntologyConfig(mode="template"), contract=retail_contract(),
                          agent=lambda *a: pytest.fail("template must make no model calls"))
    assert result.status == "completed", result.error_message
    assert result.details["modelCalls"] == 0
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["sourceFiles"] and manifest["artifacts"]["contract.json"]
    graph = Dataset(default_union=True)
    graph.parse(result.nquads_path, format="nquads")
    assert len(graph) > 0
    from dataelf.ontology.artifacts import validate_result
    assert validate_result(result) is None
    Path(result.nquads_path).write_text("tampered")
    assert "hash mismatch" in validate_result(result)


def test_invalid_template_never_falls_back_or_publishes(tmp_path):
    request = retail_request(tmp_path)
    (tmp_path / "orders.json").write_text('[{"buyer":{"id":"c1"}}]')
    result = run_ontology(request, config=OntologyConfig(mode="template"), contract=retail_contract(), agent=lambda *a: pytest.fail())
    assert result.status == "failed" and "identity" in result.error_message
    assert not (tmp_path / "ontology/published").exists()


def test_dynamic_independent_reviews_and_cross_stage_repair(tmp_path):
    calls=[]
    def agent(role,context,directory,role_config):
        calls.append((role,directory))
        if role=='modeler':return retail_contract().model_dump(mode='json')
        if len(calls)==2:return {'verdict':'revise','summary':'recheck identity','issues':[{'severity':'high','target':'contract','message':'identity','evidence':['customers.csv#/0'],'required_change':'recheck','acceptance_criteria':'inspected'}]}
        return {'verdict':'approve','summary':'checked','issues':[]}
    result=run_ontology(retail_request(tmp_path),config=OntologyConfig(),agent=agent)
    assert result.status=='completed',result.error_message
    assert [r for r,_ in calls]==['modeler','reviewer']*2
    assert len({d for _,d in calls})==4
    assert result.details['metrics']['repairs']==1


def test_budget_timeout_cancellation_and_source_changes(tmp_path):
    from dataelf.discovery.run_control import RunCancelled
    request = retail_request(tmp_path)
    result = run_ontology(request, config=OntologyConfig(max_repairs=1), agent=lambda *a: {})
    assert result.status == "failed" and "budget exhausted" in result.error_message
    def timeout(*a):
        raise TimeoutError("test timeout")
    assert run_ontology(request, config=OntologyConfig(), agent=timeout).status == "incomplete"
    def cancel(*a):
        raise RunCancelled("cancelled")
    with pytest.raises(RunCancelled):
        run_ontology(request, config=OntologyConfig(), agent=cancel)
    catalog = SourceCatalog(tmp_path, request.sources)
    (tmp_path / "customers.csv").write_text("id,name\nc1,Changed\n")
    with pytest.raises(ValueError, match="source changed"):
        materialize(retail_contract(), catalog)


def test_schema_operations_cardinality_and_queries_are_executable(tmp_path):
    request = retail_request(tmp_path)
    contract = retail_contract()
    contract.mappings[0].nodes[0].iri = {"op": "invent_business_identity"}
    with pytest.raises(ValueError, match="unsupported expression"):
        validate_contract(contract, {"customers", "orders"})
    contract = retail_contract()
    contract.constraints[0].min_count = 2
    contract.constraints[0].max_count = 2
    _, _, validation = materialize(contract, SourceCatalog(tmp_path, request.sources))
    assert validation["status"] == "invalid"
    contract.queries[0].query = "SELECT * WHERE { SERVICE <https://example.invalid> {?s ?p ?o} }"
    with pytest.raises(ValueError, match="local"):
        materialize(contract, SourceCatalog(tmp_path, request.sources))


def test_reviewer_cannot_execute_or_write_modeling_artifacts(tmp_path):
    from dataelf.ontology.tools import dispatch
    request = retail_request(tmp_path)
    catalog = SourceCatalog(tmp_path, request.sources)
    context = tmp_path / "review/context.json"
    write_json(context, {"role": "reviewer", "request": request.model_dump(mode="json"), "source_fingerprint": catalog.fingerprint})
    with pytest.raises(ValueError, match="reviewers cannot execute"):
        dispatch(context, "ontology_execute", {})
    with pytest.raises(ValueError, match="unknown ontology tool"):
        dispatch(context, "bash", {"command": "touch bad"})
    with pytest.raises(ValueError):
        dispatch(context, "ontology_submit", retail_contract().model_dump(mode="json"))
    assert not (context.parent / "submission.json").exists()


def test_config_defaults_disabled_legacy_conflicts_and_relative_templates(tmp_path):
    from dataelf.config import DataElfConfig
    assert not DataElfConfig().ontology.enabled
    config = tmp_path / "ontology.yaml"
    config.write_text("mode: template\ntemplates: [templates/test.json]\n")
    assert load_config(config).templates == [tmp_path / "templates/test.json"]
    with pytest.raises(ValueError, match="conflicting"):
        DataElfConfig(ontology={"enabled": False}, domains={"ai_index": {"modeling": {"enabled": True}}}).ontology_for_domain("ai_index")
    legacy = DataElfConfig(domains={"ai_index": {"modeling": {"enabled": True}}})
    with pytest.warns(DeprecationWarning):
        assert legacy.ontology_for_domain("ai_index").enabled
    assert not legacy.ontology_for_domain("retail").enabled


def test_fixed_templates_compose_strictly(tmp_path):
    from dataelf.ontology.templates import load_templates
    first = retail_contract()
    second = first.model_copy(deep=True)
    first.mappings = first.mappings[:1]
    second.mappings = second.mappings[1:]
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    for path, value in zip(paths, [first, second]):
        write_json(path, OntologyTemplate(domain="retail", contract=value).model_dump(mode="json"))
    assert len(load_templates(paths, domain="retail").contract.mappings) == 2
    second.properties["name"].range = "xsd:integer"
    write_json(paths[1], OntologyTemplate(domain="retail", contract=second).model_dump(mode="json"))
    with pytest.raises(ValueError, match="conflicting"):
        load_templates(paths, domain="retail")


def test_reviewer_receives_candidate_graph_and_contract_binding(tmp_path):
    roles=[]
    def agent(role,context,*_):
        roles.append(role)
        if role=='modeler':return retail_contract().model_dump(mode='json')
        assert context['candidate_hash'] and Path(context['graph_path']).is_file()
        assert context['graph_ref']['workspace_id']
        return {'verdict':'approve','summary':'checked','issues':[]}
    result=run_ontology(retail_request(tmp_path),config=OntologyConfig(),agent=agent)
    assert result.status=='completed',result.error_message
    assert roles==['modeler','reviewer']


def test_review_recovery_retains_candidate_and_effective_model(tmp_path):
    from types import SimpleNamespace
    from dataelf.ontology.errors import OntologyError
    from dataelf.ontology.config import RoleConfig
    roles=[];hashes=[]
    def agent(role,context,*_):
        roles.append(role)
        if role=='modeler':return retail_contract().model_dump(mode='json')
        hashes.append(context['candidate_hash'])
        if len(hashes)==1:
            raise OntologyError('ONTOLOGY_OUTPUT_TRUNCATED','control',[{'recoverable':True}])
        return {'verdict':'approve','summary':'verified after resume','issues':[]}
    agent.config=SimpleNamespace(explorer=SimpleNamespace(pi=SimpleNamespace(model='provider/inherited')))
    result=run_ontology(retail_request(tmp_path),config=OntologyConfig(max_repairs=0),agent=agent)
    assert result.status=='completed',result.error_message
    assert roles==['modeler','reviewer','reviewer'] and hashes[0]==hashes[1]
    manifest=json.loads(Path(result.manifest_path).read_text())
    assert manifest['reviewer_config']['model']=='provider/inherited'
    assert result.details['metrics']['repairs']==0 and result.details['metrics']['controlRestarts']==1
    other=tmp_path/'explicit';other.mkdir()
    result=run_ontology(retail_request(other),config=OntologyConfig(reviewer=RoleConfig(model='provider/explicit')),agent=agent)
    assert json.loads(Path(result.manifest_path).read_text())['reviewer_config']['model']=='provider/explicit'


def test_generic_domain_adapter_and_disabled_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dataelf.config import DataElfConfig
    from dataelf.discovery.contracts import DiscoveryJob, JobSpec
    from dataelf.ontology.modeler import OntologyModeler
    request = retail_request(tmp_path)
    template = tmp_path / "retail.json"
    write_json(template, OntologyTemplate(domain="retail", contract=retail_contract()).model_dump(mode="json"))
    settings = tmp_path / "ontology.yaml"
    settings.write_text("mode: template\ntemplates: [retail.json]\n")
    config = DataElfConfig(ontology={"enabled": True, "ontology_config": settings})
    spec = JobSpec(domain="retail", objective="orders", inputs={"ontology_sources": [s.model_dump(mode="json") for s in request.sources]})
    job = DiscoveryJob(job_id="retail", spec=spec, workspace_path=str(tmp_path))
    result = OntologyModeler(config).run(job, SimpleNamespace(workspace_path=str(tmp_path), artifacts=[]))
    assert result.status == "completed", result.error_message
    assert result.metrics["modelCalls"] == 0
    from dataelf.domains.ai_index.plugin import AIIndexDomainPlugin
    from dataelf.discovery.contracts import DomainManifest
    # No ontology file or model is opened while disabled.
    disabled = DataElfConfig(ontology={"enabled": False, "ontology_config": tmp_path / "missing.yaml"},
        domains={"ai_index": {"source": {"mode": "fixture", "fixtures_dir": Path(__file__).resolve().parents[1] / "fixtures/ai_index"}}})
    plugin = AIIndexDomainPlugin(disabled, SimpleNamespace(domain="ai_index"))
    assert plugin.create_modeler(JobSpec(domain="ai_index", objective="disabled"), disabled) is None
