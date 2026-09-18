from __future__ import annotations
import json
import shutil
from pathlib import Path
import pytest

from dataelf.discovery.contracts import JobSpec, ExplorerRunResult
from dataelf.discovery.run_control import RunControl
from dataelf.discovery.workflow import run_job
from dataelf_server.analysis.rdf_analysis import run_rdf_analysis
from dataelf_server.analysis.rdf_finalize import materialize_rdf_insights
from dataelf_server.analysis.validation import PipelineError
from dataelf_server.scope_v2.runner import ScopeV2Executor
from dataelf_server.scope_v2.integration import ScopeV2PrefetchResult
from dataelf_server.ontology.scope_v2_template import SOURCE_ENDPOINTS
from dataelf_server.workflows.profile import ServerProfile
from dataelf_server.workflows.explorer import ServerExplorer
from dataelf_server.workflows.pipeline import ServerPipeline, recover_workspace_insights
from tests.server.helpers import settings, FakeIntentRecognizer

FIXTURES = Path(__file__).parent / "fixtures"


def prefetch(plan, workspace):
    pages = json.loads((FIXTURES / "raw_pages.json").read_text())
    by_endpoint = {endpoint: source for source, endpoint in SOURCE_ENDPOINTS.items()}
    class Client:
        def post(self, endpoint, payload):
            rows = pages[by_endpoint[endpoint]]
            return {"code": 0, "trace_id": "offline-trace", "data": {"list": rows, "total": len(rows)}}
    result = ScopeV2Executor(client=Client(), output_root=workspace / "scope_v2").execute(plan, run_id="fixed")
    return ScopeV2PrefetchResult(result, workspace / "scope_v2/fixed/result.json", ["offline-trace"], sum(x["kept_count"] for x in result["sources"].values()))


def analyze(workspace):
    shutil.copyfile(FIXTURES / "analyze.py", workspace / "scripts/analyze_scope_v2.py")
    run_rdf_analysis(workspace)


def finalize(workspace, count=1):
    result = json.loads((workspace / "insights/candidate_signals.json").read_text())
    source_id = result["candidate_signals"][0]["source_ids"][0]
    titles = ["模型工具降低采用门槛", "开发者获得工具支持", "工具发布扩展研究选择", "采用效果仍待持续观察", "公开发布提供验证入口", "工具价值需要使用反馈"]
    return materialize_rdf_insights({"workspace": str(workspace), "insights": [{
        "title": titles[index], "thesis": titles[index] + "。模型工具发布降低开发者采用门槛，后续采用效果仍有待持续观察。",
        "source_ids": [source_id], "supporting_signal_ids": ["sig_001"],
    } for index in range(count)]})


def components(tmp_path, *, count=1, first_error=None, repair=False, no_analysis=False):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / "state")
    calls = []
    class Runner:
        def __init__(self, **kwargs):
            self.options = kwargs
        def run(self, job, context):
            workspace = Path(context.workspace_path)
            calls.append((self.options, context))
            if not no_analysis:
                if len(calls) == 1:
                    analyze(workspace)
                if not first_error or len(calls) > 1:
                    finalize(workspace, count)
            if len(calls) == 1 and repair:
                path = workspace / "insights/insight_candidates.json"
                content = json.loads(path.read_text())
                content["insight_candidates"][0]["title"] = "Wrong English title"
                path.write_text(json.dumps(content))
            return ExplorerRunResult(status="failed" if len(calls) == 1 and first_error else "completed", error_code=first_error if len(calls) == 1 else None)
    profile = ServerProfile(cfg, prefetcher=prefetch, recognizer=FakeIntentRecognizer())
    explorer = ServerExplorer(cfg, runner_factory=Runner)
    return cfg, profile, explorer, calls


@pytest.mark.parametrize("count", [1, 4, 6])
def test_scope_v2_uses_current_workflow_with_zero_model_calls_within_default_cap(tmp_path, count, monkeypatch):
    cfg, profile, explorer, calls = components(tmp_path, count=count)
    cfg.server.source.max_pages = 500
    monkeypatch.setattr("dataelf.domains.ai_index.modeling.acquisition.AIIndexRawCollector.collect", lambda *a: pytest.fail("duplicate legacy collection"))
    monkeypatch.setattr("dataelf.domains.ai_index.modeling.pipeline.run_ontology_subprocess", lambda *a: pytest.fail("Scope V2 modeling must call no model"))
    workspace = tmp_path / "attempts/0001"
    events = []
    spec = JobSpec(domain="ai_index", objective="2026-08-27 综合总结", workflow_profile="server")
    job = run_job(spec, cfg.execution_config(), plugin=profile, explorer=explorer, control=RunControl(job_id="job_shared", workspace_path=workspace, on_stage=events.append))
    assert job.status == "completed", job.error_message
    assert job.job_id == "job_shared" and Path(job.workspace_path) == workspace
    assert len(calls) == 1
    writing = json.loads((workspace / "prompts/insight_output_contract.json").read_text())
    assert writing["effective_output"]["synthesis"]["evidence_mode"] == "cross_record"
    assert writing["effective_output"]["item_count"]["max"] == 10
    assert writing["retrieval"] == job.spec.parameters["intent_input"]["retrieval"]
    assert all(call["max_pages"] == 500 for call in job.spec.parameters["scope_plan"]["calls"])
    assert events == ["initialization", "intent_recognition", "domain_prepare", "domain_prepared", "domain_modeling", "prompt_composition", "explorer", "output_validation", "domain_review", "finalization"]
    assert len(profile.result_ids(workspace)) == count
    assert len(list((workspace / "raw/ai_index").glob("*.json"))) == 5
    assert any(a.kind == "ontology_rdf" and a.path.startswith("modeling/server/") for a in job.artifacts)
    assert cfg.core.runtime.workspace_dir == Path(".dataelf")
    assert recover_workspace_insights(job.job_id, workspace, cfg)


@pytest.mark.parametrize("first_error,repair", [("PI_PROCESS_TIMEOUT", False), ("PI_EVENT_PARSE_ERROR", False), ("PI_MODEL_ERROR", False), (None, True)])
def test_one_synthesis_retry_reuses_verified_analysis(tmp_path, first_error, repair):
    cfg, profile, explorer, calls = components(tmp_path, first_error=first_error, repair=repair)
    workspace = tmp_path / "attempt"
    job = run_job(JobSpec(domain="ai_index", objective="2026-08-27 快讯模块总结", workflow_profile="server"), cfg.execution_config(), plugin=profile, explorer=explorer, control=RunControl(workspace_path=workspace))
    assert job.status == "completed", job.error_message
    assert len(calls) == 2
    assert calls[0][0]["log_prefix"] == "pi"
    assert calls[1][0]["log_prefix"] == "pi_synthesis_retry"
    assert calls[1][1].env["DATAELF_PI_SYNTHESIS_ONLY"] == "1"
    assert (workspace / "logs/pi_completion.json").is_file()
    assert (workspace / "logs/pi_synthesis_retry_completion.json").is_file()
    assert "Do not call bash" in (workspace / "prompts/synthesis_retry.md").read_text()


def test_unverified_analysis_never_gets_synthesis_retry(tmp_path):
    cfg, profile, explorer, calls = components(tmp_path, first_error="PI_PROCESS_TIMEOUT", no_analysis=True)
    job = run_job(JobSpec(domain="ai_index", objective="2026-08-27 快讯模块总结", workflow_profile="server"), cfg.execution_config(), plugin=profile, explorer=explorer)
    assert job.status == "failed"
    assert len(calls) == 1
    workspace = Path(job.workspace_path)
    assert (workspace / "reviews/quality_review.json").is_file()
    assert (workspace / "artifact_manifest.json").is_file()
    assert json.loads((workspace / "workspace_index.json").read_text())["status"] == "failed"


def test_server_requires_components_and_intent_failures_finalize(tmp_path):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / "state")
    job = run_job(JobSpec(domain="ai_index", objective="综合总结", workflow_profile="server"), cfg.execution_config())
    assert job.status == "failed" and "explicit" in job.error_message
    pipeline = ServerPipeline(cfg, profile_factory=lambda settings: ServerProfile(settings, recognizer=FakeIntentRecognizer()))
    with pytest.raises(PipelineError) as caught:
        pipeline.run(job_id="job_intent", request_payload={"query": "无效指令"}, workspace_path=tmp_path / "invalid", progress=lambda *a: None, source_trace=lambda *a: None)
    assert caught.value.code == "unsupported_scope"
    assert (tmp_path / "invalid/workspace_index.json").exists()


def test_legacy_rule_scope_is_disabled(tmp_path):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / "state")
    profile = ServerProfile(cfg, recognizer=FakeIntentRecognizer())
    with pytest.raises(PipelineError, match="archived"):
        profile.normalize_spec(JobSpec(domain="ai_index", objective="围绕智能体发现洞察", parameters={"scope": "legacy"}))


def test_result_recovery_rejects_changed_source_lineage_and_updates_core(tmp_path):
    from dataelf_server.jobs.manager import JobManager
    from dataelf_server.jobs.store import JobStore
    from tests.server.test_manager import SuccessfulPipeline
    cfg,profile,explorer,_=components(tmp_path)
    workspace=tmp_path/'state/workspaces/job_recover/attempts/0001'
    job=run_job(JobSpec(domain='ai_index',objective='2026-08-27 快讯模块总结',workflow_profile='server'),cfg.execution_config(),plugin=profile,explorer=explorer,control=RunControl(job_id='job_recover',workspace_path=workspace))
    assert job.status=='completed',job.error_message
    store=JobStore(cfg.database_path)
    store.create_job(job_id=job.job_id,trace_id='trace',request_payload={'query':job.spec.objective},workspace_path=workspace)
    store.fail_job(job.job_id,code='PI_EVENT_PARSE_ERROR',message='invalid agent output')
    manager=JobManager(cfg,store=store,pipeline=SuccessfulPipeline())
    path=workspace/'insights/insight_candidates.json'
    original=path.read_text()
    broken=json.loads(original)
    broken['insight_candidates'][0]['supporting_signals']=['unknown-signal']
    path.write_text(json.dumps(broken))
    try:
        assert manager.recover_result(job.job_id).status=='failed'
        path.write_text(original)
        assert manager.recover_result(job.job_id).status=='completed'
        assert json.loads((workspace/'workspace_index.json').read_text())['status']=='completed'
        assert json.loads((workspace/'reviews/quality_review.json').read_text())['status'] in {'pass','pass_with_warnings'}
    finally:
        manager.close(wait=True)


def test_stdout_payload_cannot_complete_a_server_job(tmp_path):
    cfg,profile,_,_ = components(tmp_path)
    fake=tmp_path/'fake-pi'
    fake.write_text('#!/usr/bin/env python3\nimport json\nprint(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"{\\\"insight_candidates\\\":[{}]}"}],"stopReason":"stop"}}))\n')
    fake.chmod(0o755)
    cfg.core.explorer.pi.binary=str(fake)
    cfg.core.explorer.pi.log_mode='quiet'
    job=run_job(JobSpec(domain='ai_index',objective='2026-08-27 快讯模块总结',workflow_profile='server'),cfg.execution_config(),plugin=profile,explorer=ServerExplorer(cfg))
    assert job.status=='failed'
    assert not (Path(job.workspace_path)/'insights/insight_candidates.json').exists()
    assert not (Path(job.workspace_path)/'prompts/synthesis_retry.md').exists()
