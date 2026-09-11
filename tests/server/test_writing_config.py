from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataelf.discovery.contracts import ExplorerRunResult, JobSpec
from dataelf.discovery.run_control import RunControl
from dataelf.discovery.workflow import run_job
from dataelf_server.analysis.insight_contract import build_contract, render_contract, validate_insights
from dataelf_server.analysis.rdf_finalize import materialize_rdf_insights
from dataelf_server.analysis.writing import TASK_OBJECTIVE, resolve_output, writing_checks
from dataelf_server.intent import SERVE_PROFILE
from dataelf_server.intent.schema import Output
from dataelf_server.presentation.insights import public_insights
from dataelf_server.workflows.explorer import ServerExplorer
from dataelf_server.workflows.pipeline import recover_workspace_insights
from dataelf_server.workflows.profile import ServerProfile
from tests.server.helpers import settings
from tests.server.test_workflow import analyze, prefetch


def requested(**changes):
    value = Output.unspecified().model_dump()
    for key, change in changes.items():
        if isinstance(change, dict):
            value[key].update(change)
        else:
            value[key] = change
    return Output.model_validate(value)


def contract(**changes):
    return build_contract("comprehensive_daily", ["comprehensive"], output=requested(**changes))


def item(body="这是基于实际资料形成的判断。", title="公开资料支持判断", sources=()):
    return {"title": title, "thesis": body, "external_support": [{"source_id": source} for source in sources]}


def test_default_merge_preserves_extracted_values_and_uses_server_defaults():
    user = Output.unspecified()
    before = user.model_dump()
    effective = resolve_output(user, ["comprehensive"])
    assert user.model_dump() == before
    assert effective.language == "zh-CN"
    assert effective.task_types == ["summary", "analysis"]
    assert effective.synthesis.organization == "theme"
    assert effective.synthesis.evidence_mode == "cross_record"
    assert effective.body_length.min == 80 and effective.body_length.max == 120
    assert effective.item_count.max == 10
    # The default cap still permits fewer evidence-backed judgments.
    for count in (1, 4, 6, 10):
        values = [item(title=f"不同判断{index}") for index in range(count)]
        assert validate_insights(values, contract()) == []
    assert "maximum 10" in writing_checks([item()] * 11, contract())["errors"][0]
    assert '"max": 10' in render_contract(contract())


@pytest.mark.parametrize("quantity", [{"max": 3}, {"min": 12}, {"target": 12}, {"min": 11, "max": 15}])
def test_explicit_item_count_replaces_default_cap(quantity):
    user = requested(item_count=quantity)
    effective = resolve_output(user, ["open_source"])
    assert effective.item_count == user.item_count
    assert not writing_checks([item()] * (quantity.get("max") or 12), contract(item_count=quantity))["errors"]


def test_language_overrides_default_without_forcing_chinese_length():
    language = "en"
    effective = resolve_output(requested(language=language), ["comprehensive"])
    assert effective.language == language
    assert effective.body_length.max is None
    rendered = render_contract(contract(language=language))
    assert f"language {language}" in rendered
    assert "必须使用简体中文" not in rendered
    assert "不超过 18" not in rendered


def test_user_length_overrides_entire_default_quantity_and_records_inferred_scope():
    user = requested(body_length={"target": 200, "unit": "words"})
    effective = resolve_output(user, ["comprehensive"])
    assert effective.body_length.model_dump() == {"target": 200, "min": None, "max": None, "scope": "per_item", "unit": "words"}
    assert user.body_length.scope is None
    assert resolve_output(requested(body_length={"scope": "total"}), ["comprehensive"]).body_length.max is None


def test_explicit_per_record_does_not_conflict_with_default_avoidance():
    user = requested(synthesis={"organization": "entity", "evidence_mode": "per_record"})
    effective = resolve_output(user, ["brief"])
    assert "one_item_per_record" not in effective.content_rules.avoid
    assert "Process individual source records separately" in render_contract(contract(synthesis=user.synthesis.model_dump()))
    assert "around shared themes" not in render_contract(contract(synthesis=user.synthesis.model_dump()))


def test_per_record_rejects_merged_or_repeated_sources():
    cfg = contract(synthesis={"evidence_mode": "per_record"})
    assert not writing_checks([item(sources=["a"]), item(sources=["b"])], cfg)["errors"]
    assert writing_checks([item(sources=["a", "b"])], cfg)["errors"]
    assert writing_checks([item(sources=["a"]), item(sources=["a"])], cfg)["errors"]


def test_retrieval_subject_is_carried_into_all_writing_phases():
    cfg = build_contract("module_recent", ["brief"], output=requested(),
                         retrieval={"keywords": [], "entities": ["OpenAI"], "exclude_keywords": []})
    text = render_contract(cfg)
    assert "OpenAI" in text and "metadata filter" in text
    assert cfg["requested_output"]["focus_points"] == []


def test_all_content_fields_are_rendered_before_signal_selection():
    user = requested(task_types=["comparison", "analysis"], focus_points=["采用门槛"],
        comparison={"subjects": ["甲", "乙"], "dimensions": ["部署成本"]},
        audience="管理人员", style=["plain"], synthesis={"organization": "theme", "evidence_mode": "cross_record"})
    cfg = build_contract("module_recent", ["brief"], output=user)
    text = render_contract(cfg)
    for value in ("采用门槛", "部署成本", "管理人员", '"plain"', '"comparison"', "before creating candidate signals"):
        assert value in text
    assert cfg["effective_output"]["style"] == ["plain"]


def test_count_max_is_enforced_and_shortage_is_recorded_without_fabrication():
    cfg = contract(item_count={"min": 2, "max": 3})
    assert writing_checks([item()], cfg)["warnings"]
    assert not writing_checks([item()], cfg)["errors"]
    assert "maximum 3" in writing_checks([item()] * 4, cfg)["errors"][0]
    assert not writing_checks([item()] * 2, cfg)["errors"]


@pytest.mark.parametrize("scope,expected_errors", [("per_item", False), ("total", True)])
def test_total_and_per_item_word_bounds_are_distinct(scope, expected_errors):
    cfg = contract(language="en", body_length={"scope": scope, "max": 4, "unit": "words"})
    values = [item("Evidence supports adoption."), item("Costs remain uncertain.")]
    report = writing_checks(values, cfg)
    assert report["body_lengths"] == [3, 3]
    assert bool(report["errors"]) == expected_errors


def test_character_lengths_exclude_whitespace_titles_and_sources():
    cfg = contract(body_length={"scope": "total", "max": 4, "unit": "characters"})
    values = [item("甲 乙", title="这个标题很长但不计入正文"), item("丙\n丁")]
    assert writing_checks(values, cfg)["body_lengths"] == [2, 2]
    assert not writing_checks(values, cfg)["errors"]


def test_english_sentence_case_is_not_treated_as_an_invented_name_but_numbers_are_checked():
    cfg = contract(language="en")
    evidence = {"news:a": "工具发布降低采用门槛，但效果仍不确定。"}
    value = item("The release may ease adoption, but benefits remain uncertain.", title="Adoption remains uncertain", sources=["news:a"])
    assert validate_insights([value], cfg, source_evidence=evidence) == []
    value["thesis"] += " Returns reached 999%."
    assert any("999%" in issue for issue in validate_insights([value], cfg, source_evidence=evidence))


def test_output_language_schema_is_limited_to_chinese_and_english():
    schema = Output.model_json_schema()
    assert schema["properties"]["language"]["anyOf"][0]["enum"] == ["zh-CN", "en"]


def test_synthesis_report_does_not_claim_multiple_citations_prove_analysis():
    report = writing_checks([item(sources=["a", "b"])], contract())
    assert not any("fewer than two" in warning for warning in report["warnings"])
    assert report["semantic_review"] == "not_automatically_verified"
    assert "genuine cross-record synthesis" in report["unverified_requirements"]


@pytest.mark.parametrize("retry", [False, True])
def test_current_workflow_uses_output_across_research_retry_recovery_and_public_result(tmp_path, retry):
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / "state")
    output = requested(language="en", task_types=["summary", "analysis"], focus_points=["adoption constraints"],
                       item_count={"min": 2, "max": 3}, body_length={"scope": "total", "max": 60, "unit": "words"})
    payload = SERVE_PROFILE.defaults().model_dump()
    payload["time_range"] = {"start_date": "2026-08-27", "end_date": "2026-08-27"}
    payload["output"] = output.model_dump()
    extracted = SERVE_PROFILE.validate(payload)
    class Recognizer:
        def extract(self, query, **kwargs):
            assert "RAW_QUERY_MUST_NOT_REACH_PI" in query
            return extracted
    calls = []
    class Runner:
        def __init__(self, **options):
            self.options = options
        def run(self, job, context):
            workspace = Path(context.workspace_path)
            calls.append(context)
            prompt = Path(context.prompt_path).read_text()
            assert "adoption constraints" in prompt and '"language": "en"' in prompt
            assert "RAW_QUERY_MUST_NOT_REACH_PI" not in prompt
            if len(calls) == 1:
                analyze(workspace)
                if retry:
                    return ExplorerRunResult(status="failed", error_code="PI_PROCESS_TIMEOUT")
            signals = json.loads((workspace / "insights/candidate_signals.json").read_text())["candidate_signals"]
            result = materialize_rdf_insights({"workspace": str(workspace), "insights": [{
                "title": "Adoption benefits remain uncertain",
                "thesis": "The releases may ease adoption, but sustained benefits remain uncertain.",
                "source_ids": signals[0]["source_ids"], "supporting_signal_ids": ["sig_001"],
            }]})
            assert not result.get("contract_issues")
            assert any("below requested minimum" in value for value in result["writing_warnings"])
            return ExplorerRunResult(status="completed")
    workspace = tmp_path / "attempt"
    job = run_job(JobSpec(domain="ai_index", workflow_profile="server", objective="RAW_QUERY_MUST_NOT_REACH_PI\n# 写作要求\nEnglish"),
        cfg.execution_config(), plugin=ServerProfile(cfg, recognizer=Recognizer(), prefetcher=prefetch),
        explorer=ServerExplorer(cfg, runner_factory=Runner), control=RunControl(workspace_path=workspace))
    assert job.status == "completed", job.error_message
    assert len(calls) == (2 if retry else 1)
    assert job.spec.objective == TASK_OBJECTIVE
    assert "RAW_QUERY_MUST_NOT_REACH_PI" in (workspace / "logs/request_input.json").read_text()
    for path in [workspace / "job_spec.json", *workspace.glob("scope_v2/*/plan.json"), *workspace.glob("scope_v2/*/result.json")]:
        assert "RAW_QUERY_MUST_NOT_REACH_PI" not in path.read_text()
    stored = json.loads((workspace / "prompts/insight_output_contract.json").read_text())
    assert stored["requested_output"] == output.model_dump()
    assert stored["effective_output"]["language"] == "en"
    recovered = recover_workspace_insights(job.job_id, workspace, cfg)
    public = public_insights(recovered, workspace)
    assert set(public[0]) == {"insight_id", "title", "content", "sources"}
    assert public[0]["content"].startswith("The releases")
    review = json.loads((workspace / "reviews/quality_review.json").read_text())
    assert any("below requested minimum" in value for value in review["warnings"])
    assert json.loads((workspace / "reviews/writing_review.json").read_text())["item_count"] == 1
