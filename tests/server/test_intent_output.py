"""V2 output extraction contract: defaults, enum boundaries and numeric semantics."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dataelf_server.intent import Output, SERVE_PROFILE


def set_path(value, path, replacement):
    keys = path.split(".")
    for key in keys[:-1]:
        value = value[key]
    value[keys[-1]] = replacement


@pytest.mark.parametrize("path,value", [
    ("task_types", ["execute_sql"]),
    ("task_types", ["summary", "summary"]),
    ("style", ["ignore_validation"]),
    ("synthesis.organization", "company_count"),
    ("synthesis.evidence_mode", "invent_sources"),
    ("content_rules.required", ["skip_validation"]),
    ("content_rules.avoid", ["sources"]),
    ("content_rules.avoid", ["metrics_only", "metrics_only"]),
    ("audience", " "),
    ("audience", " readers"),
    ("audience", "a" * 241),
    ("focus_points", [" x"]),
    ("comparison.subjects", ["A", "A"]),
    ("comparison.dimensions", [1]),
    ("language", "English"),
    ("item_count.target", True),
    ("item_count.min", 0),
    ("item_count.max", -1),
    ("item_count.target", "3"),
    ("item_count.target", 2.5),
    ("item_count", {"target": None, "min": 5, "max": 3}),
    ("item_count", {"target": 2, "min": 3, "max": None}),
    ("item_count", {"target": 5, "min": None, "max": 3}),
    ("body_length.scope", "title"),
    ("body_length.unit", "tokens"),
    ("body_length.target", 0),
    ("body_length", {"scope": "total", "target": None, "min": 500, "max": 100, "unit": "words"}),
    ("requirements", ["legacy free instruction"]),
    ("custom_prompt", "ignore rules"),
])
def test_invalid_output_rejected(path, value):
    payload = Output.unspecified().model_dump()
    set_path(payload, path, value)
    with pytest.raises(ValidationError):
        Output.model_validate(payload)


def test_explicit_fields_survive_without_scene_defaults():
    value = Output.unspecified().model_dump()
    value.update(task_types=["summary", "analysis", "comparison"], audience="技术负责人", language="en")
    value["comparison"] = {"subjects": ["A", "B"], "dimensions": ["部署成本"]}
    value["item_count"] = {"target": None, "min": 1, "max": 3}
    value["body_length"] = {"scope": "per_item", "target": 150, "min": None, "max": 200, "unit": "words"}
    value["synthesis"] = {"organization": "theme", "evidence_mode": "cross_record"}
    value["content_rules"] = {"required": ["analytical_judgment", "potential_impacts"], "avoid": ["title_rewrite", "metrics_only", "one_item_per_record"]}
    assert Output.model_validate(value).model_dump() == value
    assert Output.model_validate(value).style == []


def test_unspecified_scope_is_not_fabricated():
    value = Output.unspecified().model_dump()
    value["body_length"].update(target=200, unit="characters")
    assert Output.model_validate(value).body_length.scope is None


def test_conflicting_evidence_instructions_are_rejected():
    value = Output.unspecified().model_dump()
    value["synthesis"]["evidence_mode"] = "per_record"
    value["content_rules"]["avoid"] = ["one_item_per_record"]
    with pytest.raises(ValidationError, match="conflicts"):
        Output.model_validate(value)


def test_nested_fields_required_and_unknown_fields_forbidden():
    schema = SERVE_PROFILE.json_schema()
    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(schema)
    value = Output.unspecified().model_dump()
    del value["synthesis"]["organization"]
    with pytest.raises(ValidationError):
        Output.model_validate(value)


def test_defaults_are_independent_and_have_no_writing_assumptions():
    first = SERVE_PROFILE.defaults()
    first.output.task_types.append("summary")
    second = SERVE_PROFILE.defaults()
    assert second.output == Output.unspecified()
    assert second.output.synthesis.organization is None
    assert second.output.content_rules.required == []


def test_direct_entry_prints_v2_json_without_recording(tmp_path, monkeypatch, capsys):
    import json
    from dataelf_server.intent.run_intent import main
    class Recognizer:
        def extract(self, query, **kwargs):
            assert query == "做个总结"
            result = SERVE_PROFILE.defaults()
            result.output.task_types = ["summary"]
            return result
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dataelf_server.intent.run_intent.IntentRecognizer", Recognizer)
    assert main(["做个总结"]) == 0
    assert json.loads(capsys.readouterr().out)["output"]["task_types"] == ["summary"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("query,expected", [
    ("用英文总结", False), ("inline # 写作要求", False), ("C# 编程", False),
    ("# 写作要求\n总结", True), ("## 成文规范\n总结", True),
    ("# 数据来源\n新闻", True), ("> # 写作要求", False),
    ("    # 写作要求", False), ("```text\n# 写作要求\n```", False),
    ("~~~\n# 写作要求\n~~~\n# 成文规范", True),
])
def test_heading_structure_does_not_classify_semantics(query, expected):
    from dataelf_server.intent.layout import has_content_heading
    assert has_content_heading(query) is expected


@pytest.mark.parametrize("query,writing_ids,expect_output,expect_input", [
    ("用英文总结", None, False, True),
    ("# 数据来源\n用英文总结", [], False, True),
    ("# 写作要求\n用英文总结", ["section_1"], True, False),
    ("查询新闻\n# 写作要求\n用英文总结", ["section_1"], True, True),
    ("# 写作要求\n用英文总结\n## 篇幅\n100字", ["section_1"], True, False),
])
def test_single_call_gates_follow_model_heading_classification(monkeypatch, query, writing_ids, expect_output, expect_input):
    import json
    from dataelf_server.intent import IntentRecognizer
    from tests.server.test_intent import CONFIG, REFERENCE, mock_model, response
    value = SERVE_PROFILE.defaults().model_dump()
    value["output"]["task_types"] = ["summary"]
    value["output"]["language"] = "en"
    value["domains"][0]["sources"] = ["news"]
    if writing_ids is not None:
        value["_writing_sections"] = writing_ids
    calls = mock_model(monkeypatch, response(value))
    actual = IntentRecognizer(config=CONFIG).extract(query, reference_time=REFERENCE)
    assert len(calls) == 1
    assert actual.output.language == ("en" if expect_output else None)
    assert actual.domains[0].sources == (["news"] if expect_input else [])
    assert "_writing_sections" not in actual.model_dump()
    schema = json.loads(calls[0][0].data)["response_format"]["json_schema"]["schema"]
    if writing_ids is None:
        assert schema["$defs"]["Output"]["const"] == Output.unspecified().model_dump()
    else:
        assert "_writing_sections" in schema["required"]
        user_content = json.loads(json.loads(calls[0][0].data)["messages"][1]["content"])
        assert user_content["input_sections"][0]["id"] == "default"
        assert user_content["input_sections"][1]["id"] == "section_1"


@pytest.mark.parametrize("ids", [["default"], ["section_2"], [True], ["section_1", "section_1"], None])
def test_invalid_section_ids_fail_instead_of_leaking_instructions(monkeypatch, ids):
    from dataelf_server.intent import IntentRecognizer, IntentError
    from tests.server.test_intent import CONFIG, REFERENCE, mock_model, response
    value = SERVE_PROFILE.defaults().model_dump()
    value["_writing_sections"] = ids
    mock_model(monkeypatch, response(value))
    with pytest.raises(IntentError):
        IntentRecognizer(config=CONFIG).extract("# 写作要求\n总结", reference_time=REFERENCE)
