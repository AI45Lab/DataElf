from __future__ import annotations

import pytest
from pydantic import ValidationError

from dataelf_server.presentation.schemas import InsightQueryRequest


def test_natural_language_query_is_trimmed() -> None:
    request = InsightQueryRequest(query="  围绕 Agentic LLMs 发现 3 个 insight  ")
    assert request.model_dump(exclude_none=True) == {
        "query": "围绕 Agentic LLMs 发现 3 个 insight",
        "scope": "scope_v2",
    }


@pytest.mark.parametrize("value", ["pi", "pi_ontology", "unknown"])
def test_query_request_rejects_explorer_selection(value: str) -> None:
    with pytest.raises(ValidationError):
        InsightQueryRequest(query="test", insights_explorer=value)


@pytest.mark.parametrize("value", ["scope_v2"])
def test_query_request_accepts_current_scope(value: str) -> None:
    request = InsightQueryRequest(query="test", scope=value)
    assert request.scope == value


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "ok", "insights_explorer": "unknown"},
        {"query": "ok", "scope": "unknown"},
        {"query": "ok", "scope": "legacy"},
        {"query": "ok", "unknown": True},
    ],
)
def test_query_request_rejects_invalid_values(payload: dict) -> None:
    with pytest.raises(ValidationError):
        InsightQueryRequest.model_validate(payload)
