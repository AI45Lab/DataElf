from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataelf_server.analysis.insight_contract import write_contract
from dataelf_server.analysis.rdf_analysis import run_rdf_analysis, validate_analysis_manifest
from dataelf_server.analysis.rdf_finalize import RDFFinalizeError, materialize_rdf_insights


def _prepare_verified_analysis(
    workspace: Path,
    source_ids: list[str],
) -> list[dict[str, str]]:
    items = [
        {
            "source": source_id.split(":", 1)[0],
            "source_id": source_id,
            "title": f"来源 {index}",
            "url": f"https://example.com/{index}",
            "published_at": "2026-09-01T00:00:00+08:00",
            "data": {"text": f"来源 {index} 的人工智能证据内容。"},
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    result_path = workspace / "scope_v2" / "run" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {"sources": {"test": {"items": items}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    graph = workspace / "modeling" / "server" / "graph.nq"
    graph.parent.mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts").mkdir(exist_ok=True)
    (workspace / "artifacts/server_inputs.json").write_text(json.dumps({"rdf_path": "modeling/server/graph.nq"}))
    graph.write_text(
        "".join(
            f'<urn:test:entity:{index}> <urn:dataelf:ontology:ai-index:sourceId> "{source_id}" <urn:dataelf:ontology:ai-index:graph/domain> .\n'
            for index, source_id in enumerate(source_ids, start=1)
        ),
        encoding="utf-8",
    )
    script = workspace / "scripts" / "analyze_scope_v2.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    embedded = json.dumps(items, ensure_ascii=False)
    script.write_text(
        f'''from __future__ import annotations
import csv
import json
import os
from pathlib import Path
from rdflib import Dataset

workspace = Path(__file__).resolve().parents[1]
items = json.loads({embedded!r})
dataset = Dataset()
dataset.parse(workspace / json.loads((workspace / "artifacts/server_inputs.json").read_text())["rdf_path"], format="nquads")
rdf_subjects = {{str(value): str(subject) for subject, predicate, value, graph in dataset.quads((None, None, None, None)) if str(predicate).endswith("sourceId")}}
(workspace / "tables").mkdir(exist_ok=True)
with (workspace / "tables" / "source_analysis.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=["source_id", "source", "title", "url", "rdf_subject", "finding"])
    writer.writeheader()
    for item in items:
        writer.writerow({{"source_id": item["source_id"], "source": item["source"], "title": item["title"], "url": item["url"], "rdf_subject": rdf_subjects[item["source_id"]], "finding": "verified trend"}})
(workspace / "notes").mkdir(exist_ok=True)
credential_visible = bool(os.getenv("OPENAI_API_KEY") or os.getenv("AI_INDEX_API_KEY"))
(workspace / "notes" / "rdf_analysis.md").write_text(f"本分析逐条核验 Scope V2 来源，并将来源级观察映射为候选信号。credential_visible={{credential_visible}}\\n", encoding="utf-8")
(workspace / "deep_dives").mkdir(exist_ok=True)
signals = []
for index, item in enumerate(items, start=1):
    signal_id = f"sig_{{index:03d}}"
    dive = f"deep_dives/{{signal_id}}.md"
    (workspace / dive).write_text(f"# 候选信号 {{index}}\\n\\n该信号基于 {{item['source_id']}} 的来源级分析。\\n", encoding="utf-8")
    signals.append({{"signal_id": signal_id, "summary": f"来源 {{index}} 呈现可验证趋势", "why_might_matter": "影响产业判断", "source_ids": [item["source_id"]], "analysis_artifacts": ["tables/source_analysis.csv", dive], "related_entities": ["AI", item["source"]]}})
(workspace / "insights").mkdir(exist_ok=True)
(workspace / "insights" / "candidate_signals.json").write_text(json.dumps({{"candidate_signals": signals}}, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
''',
        encoding="utf-8",
    )
    run_rdf_analysis(workspace)
    assert validate_analysis_manifest(workspace) == []
    assert "credential_visible=False" in (
        workspace / "notes" / "rdf_analysis.md"
    ).read_text(encoding="utf-8")
    return items


def _compact_insight(
    index: int,
    source_id: str,
    *,
    thesis: str | None = None,
) -> dict[str, object]:
    return {
        "insight_id": f"ins_{index:03d}",
        "title": f"洞察{index}",
        "thesis": thesis or f"这是第{index}条有来源支持的洞察正文。",
        "source_ids": [source_id],
        "supporting_signal_ids": [f"sig_{index:03d}"],
        "counterargument": "样本仍然有限。",
        "confidence": 0.7,
    }


def test_finalizer_rejects_unverified_analysis(tmp_path: Path) -> None:
    with pytest.raises(RDFFinalizeError, match="verified Pi analysis"):
        materialize_rdf_insights(
            {
                "workspace": str(tmp_path),
                "insights": [_compact_insight(1, "news:1")],
            }
        )


def test_analysis_manifest_rejects_script_tampering(tmp_path: Path) -> None:
    _prepare_verified_analysis(tmp_path, ["news:1"])
    script = tmp_path / "scripts" / "analyze_scope_v2.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    assert validate_analysis_manifest(tmp_path) == [
        "Pi analysis script changed after its verified execution."
    ]


def test_analysis_normalizes_mechanical_artifact_failures(tmp_path: Path) -> None:
    _prepare_verified_analysis(tmp_path, ["news:1"])
    script = tmp_path / "scripts" / "analyze_scope_v2.py"
    script.write_text(
        '''from __future__ import annotations
import csv
import json
from pathlib import Path

workspace = Path(__file__).resolve().parents[1]
table = workspace / "tables" / "source_analysis.csv"
with table.open("r", encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle))
    headers = list(rows[0])
rows[0]["rdf_subject"] = "instance/entity/news:1"
with table.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
signals_path = workspace / "insights" / "candidate_signals.json"
document = json.loads(signals_path.read_text(encoding="utf-8"))
document["candidate_signals"][0]["analysis_artifacts"] = [
    "tables/source_analysis.csv", "deep_dives/missing.md"
]
document["candidate_signals"].append({
    "signal_id": "sig_empty",
    "summary": "没有来源的机械性无效信号",
    "source_ids": [],
    "analysis_artifacts": ["tables/source_analysis.csv"],
})
signals_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
for path in (workspace / "deep_dives").glob("*.md"):
    path.unlink()
raise RuntimeError("presentation failed after analytical outputs were written")
''',
        encoding="utf-8",
    )

    manifest = run_rdf_analysis(tmp_path)

    assert manifest["script_exit_code"] == 1
    assert manifest["signal_count"] == 1
    assert validate_analysis_manifest(tmp_path) == []
    rows = list(
        __import__("csv").DictReader(
            (tmp_path / "tables" / "source_analysis.csv").open(
                "r", encoding="utf-8", newline=""
            )
        )
    )
    assert rows[0]["rdf_subject"] == "urn:test:entity:1"
    signals = json.loads(
        (tmp_path / "insights" / "candidate_signals.json").read_text(
            encoding="utf-8"
        )
    )["candidate_signals"]
    assert [signal["signal_id"] for signal in signals] == ["sig_001"]
    assert (tmp_path / "deep_dives" / "sig_001.md").is_file()


def test_analysis_rebuilds_empty_source_table_from_cited_rdf_sources(
    tmp_path: Path,
) -> None:
    _prepare_verified_analysis(tmp_path, ["news:1", "youtube:2"])
    script = tmp_path / "scripts" / "analyze_scope_v2.py"
    script.write_text(
        '''from __future__ import annotations
import csv
from pathlib import Path

workspace = Path(__file__).resolve().parents[1]
with (workspace / "tables" / "source_analysis.csv").open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=["source_id", "source", "title", "url", "rdf_subject", "source_id"])
    writer.writeheader()
raise RuntimeError("presentation failed after signals were selected")
''',
        encoding="utf-8",
    )

    manifest = run_rdf_analysis(tmp_path)

    assert manifest["script_exit_code"] == 1
    assert manifest["signal_count"] == 2
    assert manifest["source_count"] == 2
    assert validate_analysis_manifest(tmp_path) == []
    rows = list(
        __import__("csv").DictReader(
            (tmp_path / "tables" / "source_analysis.csv").open(
                "r", encoding="utf-8", newline=""
            )
        )
    )
    assert {row["source_id"] for row in rows} == {"news:1", "youtube:2"}
    assert all(row["rdf_subject"].startswith("urn:test:entity:") for row in rows)


def test_finalizer_only_writes_results_after_verified_analysis(
    tmp_path: Path,
) -> None:
    source_ids = [f"news:{index}" for index in range(1, 5)]
    _prepare_verified_analysis(tmp_path, source_ids)
    signals_before = (tmp_path / "insights" / "candidate_signals.json").read_bytes()

    result = materialize_rdf_insights(
        {
            "workspace": str(tmp_path),
            "insights": [
                _compact_insight(index, source_id)
                for index, source_id in enumerate(source_ids, start=1)
            ],
        }
    )

    document = json.loads(
        (tmp_path / "insights" / "insight_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"signal_count": 4, "insight_count": 4}
    assert len(document["insight_candidates"]) == 4
    assert document["insight_candidates"][0]["supporting_signals"] == [
        "sig_001"
    ]
    assert document["insight_candidates"][0]["external_support"][0]["url"]
    assert "scripts/analyze_scope_v2.py" in document["insight_candidates"][0][
        "analysis_artifacts"
    ]
    assert (tmp_path / "insights" / "candidate_signals.json").read_bytes() == signals_before
    assert not (tmp_path / "scripts" / "named_graph_counts.rq").exists()
    assert not (tmp_path / "tables" / "rdf_named_graph_counts.csv").exists()


def test_finalizer_repairs_omitted_proving_signal_and_rejects_unproven_source(
    tmp_path: Path,
) -> None:
    _prepare_verified_analysis(tmp_path, ["news:1", "news:2"])
    with pytest.raises(RDFFinalizeError, match="unknown candidate signals"):
        value = _compact_insight(1, "news:1")
        value["supporting_signal_ids"] = ["sig_999"]
        materialize_rdf_insights({"workspace": str(tmp_path), "insights": [value]})

    value = _compact_insight(1, "news:2")
    value["supporting_signal_ids"] = ["sig_001"]
    materialize_rdf_insights({"workspace": str(tmp_path), "insights": [value]})
    insight = json.loads(
        (tmp_path / "insights" / "insight_candidates.json").read_text(
            encoding="utf-8"
        )
    )["insight_candidates"][0]
    assert insight["supporting_signals"] == ["sig_001", "sig_002"]

    signals_path = tmp_path / "insights" / "candidate_signals.json"
    signals = json.loads(signals_path.read_text(encoding="utf-8"))
    signals["candidate_signals"][1]["source_ids"] = ["news:1"]
    signals_path.write_text(json.dumps(signals), encoding="utf-8")
    manifest_path = tmp_path / "logs" / "pi_analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_ids"] = ["news:1"]
    manifest["source_count"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RDFFinalizeError, match="not supported by any verified"):
        materialize_rdf_insights({"workspace": str(tmp_path), "insights": [value]})


def test_finalizer_does_not_hard_reject_title_or_thesis_length_or_truncate(
    tmp_path: Path,
) -> None:
    _prepare_verified_analysis(tmp_path, ["news:one"])
    write_contract(
        tmp_path, mode="comprehensive_daily", modules=["comprehensive"]
    )
    long_thesis = (
        "人工智能产业进入效率竞争，企业正在调整产品与算力投入节奏，"
        "这一变化推动行业从单纯追求参数规模转向部署成本与真实应用价值，"
        "市场竞争由技术发布进一步延伸至产品交付与基础设施协同，"
        "后续资源配置将更加关注可持续回报与落地效率，"
        "企业还需要持续验证产品价值、客户需求与基础设施投入之间的平衡关系。"
    )
    value = _compact_insight(1, "news:one", thesis=long_thesis)
    value["title"] = "这是一个明显超过十八个可见字符限制的中文标题"

    result = materialize_rdf_insights(
        {"workspace": str(tmp_path), "insights": [value]}
    )

    thesis = json.loads(
        (tmp_path / "insights" / "insight_candidates.json").read_text(
            encoding="utf-8"
        )
    )["insight_candidates"][0]["thesis"]
    assert thesis == long_thesis
    contract_issues = result.get("contract_issues", [])
    assert not any("maximum is 18" in issue for issue in contract_issues)
    assert not any("required range" in issue for issue in contract_issues)


def test_finalizer_drops_only_contract_invalid_insights(tmp_path: Path) -> None:
    _prepare_verified_analysis(tmp_path, ["news:one", "news:two"])
    write_contract(tmp_path, mode="brief", modules=["brief"])
    valid = _compact_insight(
        1,
        "news:one",
        thesis="人工智能产品更新正在推动行业竞争转向实际应用与交付能力。",
    )
    invalid = _compact_insight(
        2,
        "news:two",
        thesis="人工智能产品本日增长22%，正在快速改变行业竞争格局。",
    )

    result = materialize_rdf_insights(
        {"workspace": str(tmp_path), "insights": [valid, invalid]}
    )

    assert result["insight_count"] == 1
    assert result["rejected_insight_count"] == 1
    assert result["rejected_insights"][0]["insight_id"] == "ins_002"
    assert "22%" in result["rejected_insights"][0]["issues"][0]
    assert "contract_issues" not in result
    document = json.loads(
        (tmp_path / "insights" / "insight_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["insight_id"] for item in document["insight_candidates"]] == [
        "ins_001"
    ]


def test_finalizer_keeps_retry_feedback_when_all_insights_are_invalid(
    tmp_path: Path,
) -> None:
    _prepare_verified_analysis(tmp_path, ["news:one"])
    write_contract(tmp_path, mode="brief", modules=["brief"])
    invalid = _compact_insight(
        1,
        "news:one",
        thesis="人工智能产品本日增长22%，正在快速改变行业竞争格局。",
    )

    result = materialize_rdf_insights(
        {"workspace": str(tmp_path), "insights": [invalid]}
    )

    assert result["insight_count"] == 1
    assert "22%" in result["contract_issues"][0]
    assert "rejected_insight_count" not in result


@pytest.mark.parametrize(
    ("available_ids", "supplied_id", "expected_id"),
    [
        (
            [
                "news:2c265436314036d696ed523315bc1bf1",
                "news:080e197068bb1e2b5fafa3e3437ca287",
            ],
            "news:2c2654363",
            "news:2c265436314036d696ed523315bc1bf1",
        ),
        (["twitter:2093872744383512615"], "src001", "twitter:2093872744383512615"),
    ],
)
def test_finalizer_resolves_unambiguous_model_source_aliases(
    tmp_path: Path,
    available_ids: list[str],
    supplied_id: str,
    expected_id: str,
) -> None:
    _prepare_verified_analysis(tmp_path, available_ids)
    expected_index = available_ids.index(expected_id) + 1
    value = _compact_insight(expected_index, supplied_id)

    materialize_rdf_insights(
        {"workspace": str(tmp_path), "insights": [value]}
    )

    insight = json.loads(
        (tmp_path / "insights" / "insight_candidates.json").read_text(
            encoding="utf-8"
        )
    )["insight_candidates"][0]
    assert insight["external_support"][0]["source_id"] == expected_id


@pytest.mark.parametrize('signal_document', ['{"signals": signals}', 'signals'])
def test_analysis_normalizes_bom_and_signal_root_and_still_checks_lineage(tmp_path, signal_document):
    _prepare_verified_analysis(tmp_path, ['news:001'])
    script = tmp_path / 'scripts/analyze_scope_v2.py'
    script.write_text(script.read_text().replace('encoding="utf-8", newline=""', 'encoding="utf-8-sig", newline=""'))
    script.write_text(script.read_text().replace('{"candidate_signals": signals}', signal_document))
    manifest = run_rdf_analysis(tmp_path)
    assert manifest['source_ids'] == ['news:001']
    assert validate_analysis_manifest(tmp_path) == []
    table = tmp_path / 'tables/source_analysis.csv'
    assert not table.read_bytes().startswith(b'\xef\xbb\xbf')
    assert any('root to candidate_signals' in repair for repair in manifest['normalization_repairs'])
    table.write_text(table.read_text().replace('urn:test:entity:1', 'urn:forged:subject'))
    assert any('RDF subject' in issue for issue in validate_analysis_manifest(tmp_path))
