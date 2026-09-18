"""Offline fake Pi program: derive the evidence table from the modeled graph."""
import csv
import json
from pathlib import Path
from rdflib import Dataset

workspace = Path(__file__).resolve().parents[1]
index = json.loads((workspace / "artifacts/server_inputs.json").read_text())
graph = Dataset()
graph.parse(workspace / index["rdf_path"], format="nquads")
subjects = {}
for subject, predicate, value, _ in graph.quads((None, None, None, None)):
    if str(predicate).endswith("sourceId"):
        subjects.setdefault(str(value), str(subject))
result = json.loads((workspace / index["scope_result_path"]).read_text())
items = [item for block in result["sources"].values() for item in block["items"]]
for folder in ("tables", "insights", "notes", "deep_dives"):
    (workspace / folder).mkdir(exist_ok=True)
with (workspace / "tables/source_analysis.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=["source_id", "source", "title", "url", "rdf_subject", "observation"])
    writer.writeheader()
    for item in items:
        writer.writerow({**{key: item[key] for key in ("source_id", "source", "title", "url")}, "rdf_subject": subjects[item["source_id"]], "observation": "模型工具发布降低开发者采用门槛"})
signals = [{"signal_id": "sig_001", "summary": "模型工具发布降低开发者采用门槛", "source_ids": [item["source_id"] for item in items], "analysis_artifacts": ["tables/source_analysis.csv", "deep_dives/evidence.md"]}]
(workspace / "insights/candidate_signals.json").write_text(json.dumps({"candidate_signals": signals}, ensure_ascii=False))
(workspace / "notes/rdf_analysis.md").write_text("分析通过来源标识与知识图谱实体的对应关系建立证据链，限定于本次预取记录。")
(workspace / "deep_dives/evidence.md").write_text("模型工具发布降低开发者采用门槛。证据来自本次选中的公开来源；发布行为不能单独证明规模化采用，后续应观察使用反馈和持续维护。" * 4)
