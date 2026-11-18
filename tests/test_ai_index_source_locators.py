"""Original paper URLs must remain usable as exact evidence locators."""

import json

import pytest

from dataelf.discovery.artifacts import ArtifactContractError
from dataelf.domains.ai_index.analysis_contract import validate_submission
from test_ai_index_analysis_review import setup


def report(tmp_path, support):
    context, spec, contract = setup(tmp_path)
    (tmp_path / "raw/paper.json").write_text(
        json.dumps(
            {
                "data": {
                    "list": [
                        {
                            "paper_id": "1#oai:arXiv.org:2611.11995",
                            "landing_page_url": "https://arxiv.org/abs/2611.11995",
                            "pdf_url": "http://arxiv.org/pdf/2611.11995.pdf",
                        }
                    ]
                }
            }
        )
    )
    path = tmp_path / "insights/insight_candidates.json"
    data = json.loads(path.read_text())
    data["insight_candidates"][0]["external_support"] = [support]
    path.write_text(json.dumps(data))
    return spec.model_dump(mode="json"), contract


@pytest.mark.parametrize(
    "support",
    [
        "http://arxiv.org/pdf/2611.11995.pdf",
        {"url": "http://arxiv.org/pdf/2611.11995.pdf"},
        {
            "source_file": "raw/paper.json#/data/list/0",
            "url": "http://arxiv.org/pdf/2611.11995.pdf",
        },
        {"path": "raw/paper.json#/data/list/0", "title": "Paper"},
        "1#oai:arXiv.org:2611.11995",
    ],
)
def test_actual_source_locators_accepted(tmp_path, support):
    spec, contract = report(tmp_path, support)
    assert validate_submission(tmp_path, contract, spec)[0]


@pytest.mark.parametrize(
    "support",
    [
        "http://arxiv.org/pdf/9999.99999.pdf",
        "http://arxiv.org/pdf/2611.11995.pdf (Paper)",
        {"path": "raw/paper.json#/data/list/99"},
        {"path": "../outside.json"},
        {"description": "http://arxiv.org/pdf/2611.11995.pdf"},
    ],
)
def test_nonresolvable_or_decorated_locators_still_rejected(tmp_path, support):
    spec, contract = report(tmp_path, support)
    with pytest.raises(ArtifactContractError, match=r"external_support\[0\].*exact"):
        validate_submission(tmp_path, contract, spec)
