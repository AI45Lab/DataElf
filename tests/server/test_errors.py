from __future__ import annotations

import pytest

from dataelf_server.presentation.errors import classify_job_error


@pytest.mark.parametrize(
    ("code", "message", "category", "reason", "retryable"),
    [
        ("unsupported_scope", "unknown", "intent_error", "unsupported_instruction", False),
        ("no_source_data", "empty", "source_error", "no_data", False),
        ("ai_index_network_error", "timed out", "source_error", "request_timeout", True),
        ("ai_index_http_429", "rate limit", "source_error", "rate_limited", True),
        ("PI_MODEL_ERROR", "terminated", "model_error", "model_terminated", True),
        ("pi_error", "PI_MODEL_ERROR: request timed out", "model_error", "request_timeout", True),
        ("pi_process_timeout", "Pi CLI timed out", "analysis_error", "agent_timeout", True),
        ("pi_output_incomplete", "no final output", "analysis_error", "insight_extraction_failed", True),
        ("pi_ontology_stage1_failed", "terminal_failed", "analysis_error", "ontology_build_failed", True),
        ("invalid_insights", "invalid JSON", "artifact_error", "invalid_json", True),
        ("pi_error", "insight_candidates.json is missing", "artifact_error", "output_missing", True),
        ("service_restarted", "stopped", "service_error", "service_restarted", True),
        ("EXPLORER_RUNTIME_NOT_READY", "runtime missing", "service_error", "dependency_missing", False),
        ("insight_format_invalid", "title too long", "artifact_error", "schema_invalid", False),
    ],
)
def test_classifies_public_job_errors(
    code: str,
    message: str,
    category: str,
    reason: str,
    retryable: bool,
) -> None:
    error = classify_job_error(code, message, "analyzing_with_pi")

    assert error.category == category
    assert error.reason == reason
    assert error.retryable is retryable
    assert error.message
    assert error.action


def test_error_contract_does_not_expose_internal_message() -> None:
    error = classify_job_error(
        "internal_error",
        "secret traceback at /private/workspace/file.py",
        "analyzing_with_pi",
    )

    assert "secret" not in error.message
    assert "/private" not in error.message
    assert error.details is None


def test_internal_transport_error_uses_failure_stage() -> None:
    source = classify_job_error(
        "internal_error", "[Errno 104] Connection reset by peer", "fetching_ai_index"
    )
    model = classify_job_error(
        "internal_error", "The read operation timed out", "analyzing_with_pi"
    )

    assert (source.category, source.reason) == ("source_error", "connection_failed")
    assert (model.category, model.reason) == ("model_error", "request_timeout")
