from pathlib import Path
from types import SimpleNamespace
import pytest
from dataelf.discovery import domain_services
from dataelf.ontology.collaboration import ReviewLedger


def test_local_report_review_uses_declared_domain_directories(tmp_path, monkeypatch):
    (tmp_path / "results/research").mkdir(parents=True)
    (tmp_path / "results/research/report.md").write_text("Evidence-based result")
    (tmp_path / "results/outside.md").write_text("Outside declared scope")
    (tmp_path / "insights").mkdir()
    (tmp_path / "insights/report.md").write_text("AI layout is not a generic permission")
    monkeypatch.setattr(domain_services, "services", lambda _: SimpleNamespace(writable_dirs=("results/research",)))
    monkeypatch.setattr(domain_services, "evidence", lambda _: SimpleNamespace(records=lambda: []))
    ledger = ReviewLedger(tmp_path, "analysis")
    context = {"role": "analyst", "workspace": str(tmp_path), "spec": {"domain": "fixture"}}
    item = ledger.request(context, object_kind="report", question="Check the result", evidence=[], idempotency_key="allowed", paths=["results/research/report.md"])
    assert "results/research/report.md" in item["files"]
    for path in ("insights/report.md", "results/outside.md"):
        with pytest.raises(ValueError, match="report or supporting"):
            ledger.request(context, object_kind="report", question="Check", evidence=[], idempotency_key=path, paths=[path])


def test_extensions_have_no_retired_domain_prompts():
    root = Path(__file__).resolve().parents[1] / "dataelf"
    for folder in ("discovery/runtime", "ontology/runtime"):
        for path in (root / folder).glob("*.ts"):
            text = path.read_text()
            assert "AI Index" not in text
            assert "11 contract fields" not in text
