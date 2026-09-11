from __future__ import annotations

import json

from dataelf_server.analysis.insight_contract import (
    build_contract,
    load_contract_text,
    render_contract,
    validate_insights,
    validate_workspace_insights,
    visible_char_count,
    write_contract,
)


def valid_insight(*, content: str = "人工智能" * 20) -> dict:
    return {
        "insight_id": "ins_001",
        "title": "模型竞争转向效率",
        "thesis": content,
    }


def test_comprehensive_contract_renders_common_and_module_rules(tmp_path) -> None:
    path = write_contract(
        tmp_path, mode="comprehensive_daily", modules=["comprehensive"]
    )

    text = path.read_text(encoding="utf-8")
    assert "不超过 18 个可见字符" in text
    assert "80–120 个可见字符" in text
    assert "事件 + 影响判断 + 趋势含义" in text
    assert load_contract_text(tmp_path) == text.strip()


def test_multi_module_contract_keeps_module_rules_separate() -> None:
    contract = build_contract("module_recent", ["brief", "opinion"])
    text = render_contract(contract)

    assert "快讯模块要求" in text
    assert "观点模块要求" in text
    assert "每条 Insight 按其主要来源应用对应模块要求" in text


def test_comprehensive_validator_accepts_contract_compliant_chinese() -> None:
    contract = build_contract("comprehensive_daily", ["comprehensive"])
    insight = valid_insight()

    assert visible_char_count(insight["thesis"]) == 80
    assert validate_insights([insight], contract) == []


def test_validator_rejects_language_boilerplate_and_markdown_but_not_length() -> None:
    contract = build_contract("comprehensive_daily", ["comprehensive"])
    values = [
        {
            "insight_id": "ins_001",
            "title": "This title is much too long for the contract",
            "thesis": "今日多条新闻显示\n- item",
        }
    ]

    issues = validate_insights(values, contract)

    assert not any("maximum is 18" in value for value in issues)
    assert any("title must contain Simplified Chinese" in value for value in issues)
    assert any("forbidden opening phrase" in value for value in issues)
    assert any("without Markdown" in value for value in issues)
    assert not any("required range" in value for value in issues)


def test_non_comprehensive_module_has_no_content_length_requirement() -> None:
    contract = build_contract("module_recent", ["brief"])
    value = valid_insight(content="模型发布改变企业部署节奏。")

    assert validate_insights([value], contract) == []


def test_title_and_content_lengths_are_prompt_guidance_not_a_hard_gate() -> None:
    contract = build_contract("comprehensive_daily", ["comprehensive"])
    value = valid_insight(content="模型发布改变企业部署节奏。")
    value["title"] = "这是一个明显超过十八个可见字符但仍然应当通过校验的中文标题"

    assert validate_insights([value], contract) == []


def test_workspace_validator_reads_persisted_contract(tmp_path) -> None:
    write_contract(tmp_path, mode="comprehensive_daily", modules=["comprehensive"])
    insight_dir = tmp_path / "insights"
    insight_dir.mkdir()
    (insight_dir / "insight_candidates.json").write_text(
        json.dumps({"insight_candidates": [valid_insight()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert validate_workspace_insights(tmp_path) == []


def test_workspace_validator_rejects_unsupported_numbers_and_proper_nouns(
    tmp_path,
) -> None:
    write_contract(tmp_path, mode="comprehensive_daily", modules=["comprehensive"])
    insight_dir = tmp_path / "insights"
    insight_dir.mkdir()
    value = valid_insight(
        content=(
            "Manus于2026年推出GAIA基准，推动Agent能力评估标准化并为开发者提供跨模型比较，"
            "加快相关产品形成统一采用路径与成熟生态。"
        )
    )
    value["external_support"] = [{"source_id": "news:one"}]
    (insight_dir / "insight_candidates.json").write_text(
        json.dumps({"insight_candidates": [value]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result_dir = tmp_path / "scope_v2" / "run"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "sources": {
                    "news": {
                        "items": [
                            {
                                "source_id": "news:one",
                                "title": "新加坡AI社区活动升温",
                                "data": {"text": "新加坡AI开发者活动降低软件构建门槛。"},
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    issues = validate_workspace_insights(tmp_path)

    assert any("Manus" in value and "GAIA" in value for value in issues)


def test_source_grounding_ignores_whitespace_inside_latin_name(tmp_path) -> None:
    write_contract(tmp_path, mode="module_recent", modules=["opinion"])
    insight_dir = tmp_path / "insights"
    insight_dir.mkdir()
    value = valid_insight(content="RaghuRaghuram提出人工智能需求仍会持续扩张。")
    value["external_support"] = [{"source_id": "twitter:one"}]
    (insight_dir / "insight_candidates.json").write_text(
        json.dumps({"insight_candidates": [value]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result_dir = tmp_path / "scope_v2" / "run"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "sources": {
                    "twitter": {
                        "items": [
                            {
                                "source_id": "twitter:one",
                                "title": "Raghu Raghuram discusses AI demand",
                                "data": {"text": "Raghu Raghuram discusses AI demand"},
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert validate_workspace_insights(tmp_path) == []


def test_source_grounding_accepts_equivalent_numeric_units_and_name_formatting(
    tmp_path,
) -> None:
    write_contract(tmp_path, mode="comprehensive_daily", modules=["comprehensive"])
    insight_dir = tmp_path / "insights"
    insight_dir.mkdir()
    value = valid_insight(
        content="Volta完成3亿美元融资，HappyRobot完成1.5亿美元融资，W4A8量化方案降低部署门槛。"
    )
    value["external_support"] = [{"source_id": "news:one"}]
    (insight_dir / "insight_candidates.json").write_text(
        json.dumps({"insight_candidates": [value]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result_dir = tmp_path / "scope_v2" / "run"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "sources": {
                    "news": {
                        "items": [
                            {
                                "source_id": "news:one",
                                "title": "Volta raises $300M; HappyRobot raises $150M",
                                "data": {"text": "GLM-5.2-W4A8-C8 supports efficient deployment"},
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert validate_workspace_insights(tmp_path) == []
