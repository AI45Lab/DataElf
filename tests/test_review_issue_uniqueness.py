"""Strict review identity cannot be silently overwritten by repeated records."""

from copy import deepcopy
import pytest
from dataelf.ontology.contracts import Review
from dataelf.ontology.review_loop import check_review
from dataelf.ontology.review_records import reference_schema
from dataelf.ontology.collaboration import ReviewLedger
from tests.test_ai_index_collaboration import issue


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_issue_id_is_rejected_even_when_wording_differs(changed):
    first = issue()
    second = deepcopy(first)
    if changed:
        second["message"] = "Different claim with the same ID"
    with pytest.raises(ValueError, match="Issue IDs must be unique"):
        check_review(
            Review(verdict="revise", summary="Review", issues=[first, second]),
            None,
            strict=True,
        )


def test_mixed_inline_and_saved_issue_ids_are_checked_after_assembly(
    tmp_path, monkeypatch
):
    ledger = ReviewLedger(tmp_path, "graph")
    first = issue()
    second = {**first, "message": "Reworded saved issue"}
    ident = "check-" + "a" * 64
    monkeypatch.setattr(ledger, "validate_checks", lambda *a: None)
    monkeypatch.setattr(
        ledger,
        "checks",
        lambda: [{"check_id": ident, "method": "review issue", "result": second}],
    )
    merged = ledger.assemble(
        {},
        {
            "verdict": "revise",
            "summary": "Review",
            "issues": [first],
            "issue_ids": [ident],
        },
    )
    assert len(merged["issues"]) == 2
    with pytest.raises(ValueError, match="Issue IDs must be unique"):
        check_review(Review.model_validate(merged), None, strict=True)
    saved_only = ledger.assemble(
        {}, {"verdict": "revise", "summary": "Review", "issue_ids": [ident]}
    )
    assert (
        len(check_review(Review.model_validate(saved_only), None, strict=True).issues)
        == 1
    )


def test_distinct_ids_accepted_and_legacy_unchanged():
    first = issue()
    second = {**first, "issue_id": "other"}
    assert (
        len(
            check_review(
                Review(verdict="revise", summary="Review", issues=[first, second]),
                None,
                strict=True,
            ).issues
        )
        == 2
    )
    assert (
        len(
            check_review(
                Review(verdict="revise", summary="Legacy", issues=[first, first]),
                None,
                strict=False,
            ).issues
        )
        == 2
    )


def test_reference_schema_explains_mixed_representation_without_mutating_input():
    original = Review.model_json_schema()
    before = deepcopy(original)
    schema = reference_schema(original)
    assert original == before
    assert "distinct issue_id" in schema["properties"]["issue_ids"]["description"]
