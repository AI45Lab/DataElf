from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataelf_server.presentation.insights import public_insight
from tests.server.helpers import insight


@pytest.mark.parametrize("explorer", ["pi", "pi_ontology"])
def test_public_insight_resolves_sources_for_both_explorers(
    tmp_path: Path, explorer: str
) -> None:
    workspace = tmp_path / explorer
    result_path = workspace / "scope_v2" / "run_test" / "result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "sources": {
                    "github": {
                        "items": [
                            {
                                "source_id": "github:https://github.com/acme/project",
                                "title": "Acme Project",
                                "url": "https://github.com/acme/project",
                                "data": {"title": "Acme Project"},
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    internal = insight(1)
    internal["external_support"] = [
        {
            "source_id": "github:https://github.com/acme/project",
            "url": "https://github.com/acme/project",
        }
    ]

    result = public_insight(internal, workspace)

    assert result == {
        "insight_id": "ins_001",
        "title": "Insight 1",
        "content": "Grounded thesis 1",
        "sources": [
            {
                "title": "Acme Project",
                "url": "https://github.com/acme/project",
            }
        ],
    }


def test_public_insight_prefers_embedded_source_and_removes_duplicate_urls(
    tmp_path: Path,
) -> None:
    internal = insight(1)
    internal["external_support"] = [
        {
            "title": "Readable article title",
            "url": "https://example.com/article",
            "source_id": "news_1",
        },
        {
            "title": "Duplicate title",
            "url": "https://example.com/article",
            "source_id": "news_1",
        },
        {"title": "Missing URL", "source_id": "news_2"},
    ]

    result = public_insight(internal, tmp_path)

    assert result["sources"] == [
        {"title": "Readable article title", "url": "https://example.com/article"}
    ]
    assert "supporting_signals" not in result
