"""Synthetic orchestration/review tests. Fake Pi never demonstrates LLM accuracy."""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

from dataelf.config import DataElfConfig, ExplorerConfig, PiConfig, RuntimeConfig
from dataelf.discovery.contracts import DiscoveryJob, JobSpec
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.workflow import _trace_stage, run_job
from dataelf.discovery.workspace import prepare_workspace
from dataelf.domains.trajectory_analysis.analysis import (
    ANALYSIS,
    TRACE_POINTER,
    FailureAnalysis,
    evidence_scope,
)
from dataelf.domains.trajectory_analysis.connector import (
    METADATA,
    RAW,
    SUMMARY,
    read_json,
    record_fixture,
)

FIXTURES = Path(__file__).parent / "fixtures/trajectory_analysis"


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("WT_", "AWS_", "DATAELF_", "PI_")) or any(
                marker in key for marker in ("API_KEY", "API_BASE", "BASE_URL")):
            monkeypatch.delenv(key)


def case(name):
    root = FIXTURES / name
    return json.loads((root / "input.json").read_text()), json.loads((root / "expected.json").read_text())


def configuration(tmp_path, binary=None):
    return DataElfConfig(
        runtime=RuntimeConfig(workspace_dir=tmp_path / "state", workspaces_dir=tmp_path / "jobs"),
        explorer=ExplorerConfig(pi=PiConfig(binary=str(binary) if binary else None, log_mode="quiet")),
        domains={"trajectory_analysis": {"mode": "fixture"}},
    )


def review_workspace(tmp_path, name="early_conversion"):
    calls, oracle = case(name)
    spec = JobSpec(domain="trajectory_analysis", objective=oracle["report"]["objective"])
    plugin = DomainRegistry().load_plugin(spec.domain, configuration(tmp_path))
    ws = prepare_workspace(tmp_path / "review", spec)
    for relative in plugin.manifest.workspace_dirs:
        (ws / relative).mkdir(parents=True, exist_ok=True)
    record_fixture(ws, calls)
    report = copy.deepcopy(oracle["report"])
    (ws / ANALYSIS).write_text(json.dumps(report))
    return plugin, DiscoveryJob(job_id="synthetic", spec=spec, workspace_path=str(ws)), ws, report


@pytest.mark.parametrize("name", ["early_conversion", "recovered_error", "omitted_context", "bounded_context", "no_ground_truth"])
def test_fake_pi_analysis_through_registry_and_run_job(tmp_path, name):
    calls, oracle = case(name)
    # The oracle lives only in the fake's test harness, never prepared input/prompt.
    answer = tmp_path / "fake-response.json"
    answer.write_text(json.dumps(oracle["report"]))
    binary = tmp_path / "fake-pi"
    binary.write_text(f'''#!{sys.executable} -B
import json, os
from pathlib import Path
from dataelf.domains.trajectory_analysis.connector import RAW, record_fixture, read_json
from dataelf.domains.trajectory_analysis.analysis import ANALYSIS
w = Path(os.environ['DATAELF_JOB_WORKSPACE'])
assert not (w / ANALYSIS).exists() and not (w / RAW).exists()
record_fixture(w, read_json(w, 'raw/trajectory_analysis/fixture_input.json'))
(w / ANALYSIS).write_bytes(Path({str(answer)!r}).read_bytes())
print('{{"type":"agent_end"}}')
''')
    binary.chmod(0o755)
    cfg = configuration(tmp_path, binary)
    spec = JobSpec(domain="trajectory_analysis", objective=oracle["report"]["objective"],
                   inputs={"fixture_file": str(FIXTURES / name / "input.json")},
                   parameters={"reward": 0.0, "limit": 1, "fields": ["chosen_trace"]},
                   constraints={"max_runtime_minutes": 2}, requested_outputs=["failure_analysis"])
    original = spec.model_dump()
    job = run_job(spec, cfg)
    assert spec.model_dump() == original and job.spec.model_dump() == original
    assert type(job.spec.parameters["reward"]) is float
    assert job.status == "completed"
    ws = Path(job.workspace_path)
    actual = FailureAnalysis.model_validate(read_json(ws, ANALYSIS))
    assert actual.status == oracle["expected"]["status"]
    assert actual.scope == evidence_scope(read_json(ws, RAW)["calls"])
    assert [c["envelope"] for c in read_json(ws, RAW)["calls"]] == [c["envelope"] for c in calls]
    review = read_json(ws, "reviews/quality_review.json")
    assert review["status"] == ("pass" if actual.status == "located" else "pass_with_warnings")
    assert review["metrics"]["localization_reported"] == (actual.status == "located")
    assert review["metrics"]["causal_correctness_verified"] is False
    index = read_json(ws, "workspace_index.json")
    assert index["status"] == "completed" and index["result_ids"] == ["failure_analysis"]
    inventory = read_json(ws, "artifact_manifest.json")["artifacts"]
    assert {RAW, METADATA, ANALYSIS} <= {a["path"] for a in inventory}
    assert not (ws / SUMMARY).exists()  # No new answer masquerading as old query_summary.
    prompt = (ws / "prompts/discovery_prompt.md").read_text()
    assert ANALYSIS in prompt and RAW in prompt and "fixture_input.json" in prompt
    assert str(answer) not in prompt and "expected.json" not in prompt
    assert read_json(ws, "raw/trajectory_analysis/fixture_input.json") == calls
    if actual.key_failure:
        assert actual.key_failure.evidence[0].pointer == oracle["expected"]["key_pointer"]
        assert actual.key_failure.statement not in prompt
    assert "SYNTHETIC_PRIVATE" not in (ws / ANALYSIS).read_text()


def test_default_analysis_contract_and_no_prepared_answer(tmp_path):
    cfg = configuration(tmp_path)
    plugin = DomainRegistry().load_plugin("trajectory_analysis", cfg)
    spec = JobSpec(domain="trajectory_analysis", objective="Keep this analysis objective",
                   inputs={"fixture_file": str(FIXTURES / "early_conversion/input.json")})
    normalized = plugin.normalize_spec(spec)
    assert normalized.objective == spec.objective and normalized.inputs == spec.inputs
    assert normalized.parameters == {"reward": 0, "limit": 1, "fields": ["chosen_trace"]}
    assert normalized.requested_outputs == ["failure_analysis"]
    assert spec.parameters == {} and spec.requested_outputs == []
    ws = prepare_workspace(tmp_path / "prepare", normalized)
    stage = plugin.prepare(normalized, str(ws), cfg)
    assert stage.status == "completed"
    assert not (ws / RAW).exists() and not (ws / ANALYSIS).exists()
    assert "expected" not in json.dumps(stage.context)
    assert plugin.create_modeler(normalized, cfg) is None
    contract = plugin.output_contract(normalized)
    assert {a.path for a in contract.artifacts} == {RAW, METADATA, ANALYSIS}
    assert all(a.required for a in contract.artifacts)
    with pytest.raises(ValueError, match="TRAJECTORY_OUTPUTS_UNSUPPORTED"):
        plugin.normalize_spec(spec.model_copy(update={"requested_outputs": ["query_summary"]}))


def test_prompt_is_analysis_not_query_summary(tmp_path):
    plugin = DomainRegistry().load_plugin("trajectory_analysis", configuration(tmp_path))
    prompt = plugin.build_prompt(None, None)
    for phrase in ["earliest supported", "successfully recovered", "direct cause", "alternative",
                   "JSON Pointer", "NOT a WT step_id", "not evidence of cause", "untrusted data",
                   "insufficient_evidence", "not a complete Serving record", "read_failed"]:
        assert phrase in prompt
    assert "Do not add is_session_completed" in prompt
    assert "No trajectory normalization, failure location" not in prompt


@pytest.mark.parametrize("mutation", ["other_job", "outside_path", "missing_pointer", "invalid_escape",
    "metadata_not_location", "empty_support", "whole_trace", "missing_cause", "missing_goal",
    "false_scope", "wrong_objective", "root_as_fact", "no_uncertainty",
    "wrong_result", "raw_tampered", "metadata_tampered", "raw_symlink", "report_symlink"])
def test_reject_unsupported_or_unresolvable_analysis(tmp_path, mutation):
    plugin, job, ws, report = review_workspace(tmp_path)
    ref = report["key_failure"]["evidence"][0]
    if mutation == "other_job": ref["path"] = "../other-job/" + RAW
    if mutation == "outside_path": ref["path"] = str(tmp_path / "external.json")
    if mutation == "missing_pointer": ref["pointer"] = TRACE_POINTER + "/999"
    if mutation == "invalid_escape": ref["pointer"] = TRACE_POINTER + "/~2bad"
    if mutation == "metadata_not_location": ref["pointer"] = "/calls/0/envelope/result/records/0/reward"
    if mutation == "empty_support": ref["why"] = "   "
    if mutation == "whole_trace":
        report["key_failure"]["evidence"] = [dict(ref, pointer=TRACE_POINTER)]
    if mutation == "missing_cause": report["direct_cause"] = None
    if mutation == "missing_goal": report["task_goal"] = None
    if mutation == "false_scope": report["scope"]["trace_length"] += 1
    if mutation == "wrong_objective": report["objective"] = "Different objective"
    if mutation == "root_as_fact": report["possible_root_causes"] = [dict(report["direct_cause"], basis="observed")]
    if mutation == "no_uncertainty": report["uncertainty"] = ""
    if mutation == "wrong_result": report["result_id"] = "query_summary"
    if mutation == "raw_tampered":
        raw = read_json(ws, RAW); raw["calls"][1]["arguments"]["record_id"] = "SYNTHETIC_OTHER"
        (ws / RAW).write_text(json.dumps(raw))
    if mutation == "metadata_tampered": (ws / METADATA).write_text('{"calls": []}')
    (ws / ANALYSIS).write_text(json.dumps(report))
    if mutation in {"raw_symlink", "report_symlink"}:
        target = ws / (RAW if mutation == "raw_symlink" else ANALYSIS)
        outside = tmp_path / "external.json"; outside.write_bytes(target.read_bytes())
        target.unlink(); target.symlink_to(outside)
    result = plugin.review(job, str(ws))
    assert result.status == "failed"
    assert "SYNTHETIC_OTHER" not in result.model_dump_json()


@pytest.mark.parametrize("mutation", ["located", "hidden_truncation", "invented_cause", "omit_limitation"])
def test_omitted_context_cannot_be_successful_localization(tmp_path, mutation):
    plugin, job, ws, report = review_workspace(tmp_path, "omitted_context")
    if mutation == "located": report["status"] = "located"
    if mutation == "hidden_truncation": report["scope"]["truncated"] = False
    if mutation == "invented_cause": report["direct_cause"] = case("early_conversion")[1]["report"]["direct_cause"]
    if mutation == "omit_limitation": report["limitations"].remove("truncated_output")
    (ws / ANALYSIS).write_text(json.dumps(report))
    assert plugin.review(job, str(ws)).status == "failed"


def test_schema_review_does_not_claim_causal_accuracy(tmp_path):
    plugin, job, ws, report = review_workspace(tmp_path, "recovered_error")
    oracle = case("recovered_error")[1]
    # A human would reject blaming the recovered timeout. A syntactically valid
    # claim can still pass structural review: do not call this localization accuracy.
    wrong = oracle["rejected_conclusions"][1]
    report["key_failure"]["statement"] = wrong["claim"]
    report["key_failure"]["evidence"][0]["pointer"] = wrong["pointer"]
    assert wrong["pointer"] != oracle["expected"]["key_pointer"] and wrong["reason"]
    (ws / ANALYSIS).write_text(json.dumps(report))
    reviewed = plugin.review(job, str(ws))
    assert reviewed.status == "pass" and reviewed.metrics["causal_correctness_verified"] is False


@pytest.mark.parametrize("mode,error", [("missing", "OUTPUT_CONTRACT_FAILED"),
                                       ("invalid_reference", "DOMAIN_REVIEW_FAILED")])
def test_fake_pi_report_failures_propagate_through_run_job(tmp_path, mode, error):
    _, oracle = case("early_conversion")
    report = copy.deepcopy(oracle["report"])
    report["key_failure"]["evidence"][0]["pointer"] = TRACE_POINTER + "/999"
    binary = tmp_path / "fake-pi"
    binary.write_text(f'''#!{sys.executable} -B
import json, os
from pathlib import Path
from dataelf.domains.trajectory_analysis.connector import record_fixture, read_json
from dataelf.domains.trajectory_analysis.analysis import ANALYSIS
w = Path(os.environ['DATAELF_JOB_WORKSPACE'])
record_fixture(w, read_json(w, 'raw/trajectory_analysis/fixture_input.json'))
if {mode!r} != 'missing':
    (w / ANALYSIS).write_text({json.dumps(report)!r})
print('{{"type":"agent_end"}}')
''')
    binary.chmod(0o755)
    spec = JobSpec(domain="trajectory_analysis", objective=report["objective"],
                   inputs={"fixture_file": str(FIXTURES / "early_conversion/input.json")})
    job = run_job(spec, configuration(tmp_path, binary))
    assert job.status == "failed" and job.error_code == error
    ws = Path(job.workspace_path)
    assert read_json(ws, "workspace_index.json")["result_ids"] == []
    assert (ws / "artifact_manifest.json").is_file()
    assert (ws / "reviews/quality_review.json").is_file()


def test_escaped_json_pointer_and_readonly_result_ids(tmp_path):
    plugin, job, ws, report = review_workspace(tmp_path)
    raw = read_json(ws, RAW)
    raw["calls"][1]["envelope"]["result"]["records"][0]["chosen_trace"] = {"a/b": {"~key": "synthetic observation"}}
    from dataelf.domains.trajectory_analysis.connector import project
    (ws / RAW).write_text(json.dumps(raw))
    (ws / METADATA).write_text(json.dumps({"calls": [project(c) for c in raw["calls"]]}))
    report.update(status="insufficient_evidence", scope=evidence_scope(raw["calls"]).model_dump(),
                  success_condition=None, key_failure=None, direct_cause=None, outcome=None,
                  possible_root_causes=[], other_explanations=[])
    report["task_goal"]["evidence"][0]["pointer"] = TRACE_POINTER + "/a~1b/~0key"
    report["limitations"] += ["success_condition_unknown", "outcome_unknown"]
    (ws / ANALYSIS).write_text(json.dumps(report))
    assert plugin.review(job, str(ws)).status == "pass_with_warnings"
    before = {p: (ws / p).read_bytes() for p in (RAW, METADATA, ANALYSIS)}
    assert plugin.result_ids(str(ws)) == ["failure_analysis"]
    assert all((ws / p).read_bytes() == value for p, value in before.items())


def test_domain_env_channel_vs_core_trace_with_synthetic_values(tmp_path, monkeypatch):
    skill = tmp_path / "wt-serving-query/SKILL.md"; skill.parent.mkdir(); skill.write_text("synthetic")
    fake = {"WT_SDK_DB_URI": "scheme://synthetic:FAKE_PASSWORD@invalid/db",
            "WT_SDK_S3_ENDPOINT": "https://invalid.example", "AWS_ACCESS_KEY_ID": "FAKE_ACCESS",
            "AWS_SECRET_ACCESS_KEY": "FAKE_SECRET"}
    for key, value in fake.items(): monkeypatch.setenv(key, value)
    cfg = DataElfConfig(domains={"trajectory_analysis": {"tool_python": sys.executable, "skill_path": str(skill)}})
    plugin = DomainRegistry().load_plugin("trajectory_analysis", cfg)
    spec = JobSpec(domain="trajectory_analysis", objective="synthetic")
    ws = prepare_workspace(tmp_path / "prepare", spec)
    stage = plugin.prepare(spec, str(ws), cfg)
    assert stage.status == "completed" and all(stage.env[k] == v for k, v in fake.items())
    without_env = stage.model_dump(exclude={"env"})
    assert all(value not in json.dumps(without_env) + plugin.build_prompt(None, None) for value in fake.values())
    class ObservingStore:
        def add_trace_event(self, job_id, kind, payload): self.payload = payload
    store = ObservingStore()
    job = DiscoveryJob(job_id="synthetic", spec=spec, workspace_path=str(ws))
    _trace_stage(store, job, "domain_prepare", stage)
    assert all(v not in json.dumps(store.payload) for v in fake.values())
    assert set(store.payload["env"]) == set(stage.env)
    # Preserve the completed public fix: env values are redacted at the trace boundary.
