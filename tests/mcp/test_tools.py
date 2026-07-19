from phi.mcp.tools import safe_error_summary


def test_error_summary_redacts_configured_values_before_normalizing_whitespace() -> None:
    secret = "line-one\nline-two"

    summary = safe_error_summary(RuntimeError(f"failed with {secret}"), (secret,))

    assert secret not in summary
    assert "line-one line-two" not in summary
    assert summary == "RuntimeError: failed with [redacted]"
