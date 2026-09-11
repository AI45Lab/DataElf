from dataelf_server.presentation.redaction import redact_message


def test_error_redaction_removes_known_key_shapes() -> None:
    message = "failed " + "sk-" + "example_secret " + "ak_" + "1234567890ABCDEF api_key=plain-secret"
    rendered = redact_message(message)
    assert "example_secret" not in rendered
    assert "1234567890ABCDEF" not in rendered
    assert "plain-secret" not in rendered
    assert rendered.count("[REDACTED]") == 3
