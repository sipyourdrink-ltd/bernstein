"""Unit tests for the autofix failure classifier."""

from __future__ import annotations

from bernstein.core.autofix.classifier import classify_failure


def test_security_keywords_route_to_opus() -> None:
    """CodeQL / CVE / leaked-secret signals must escalate to opus."""
    log = "ERROR: CodeQL detected potential SQL injection in module.py"
    decision = classify_failure(log)
    assert decision.kind == "security"
    assert decision.model == "opus"
    assert decision.matched_signals  # at least one pattern fired


def test_flaky_keywords_route_to_sonnet() -> None:
    """Timeout / flaky signals route to sonnet."""
    log = "PASSED test_a, FAILED test_b: timeout exceeded after 30s"
    decision = classify_failure(log)
    assert decision.kind == "flaky"
    assert decision.model == "sonnet"


def test_config_keywords_route_to_haiku() -> None:
    """Lint / format / config signals route to haiku."""
    log = "ruff check failed: E501 line too long\nblack would reformat foo.py"
    decision = classify_failure(log)
    assert decision.kind == "config"
    assert decision.model == "haiku"


def test_unknown_log_falls_back_to_config_haiku() -> None:
    """Unrecognised text falls back to the cheap arm, never opus."""
    decision = classify_failure("nothing intelligible here")
    assert decision.kind == "config"
    assert decision.model == "haiku"


def test_security_takes_priority_over_flaky() -> None:
    """A log mixing flaky+security tokens must escalate to security."""
    log = "test_x: timeout. CodeQL: cve-2023-1234 vulnerability detected"
    decision = classify_failure(log)
    assert decision.kind == "security"
    assert decision.model == "opus"


def test_empty_log_is_handled_gracefully() -> None:
    """An empty log returns the default config bucket without raising."""
    decision = classify_failure("")
    assert decision.kind == "config"
    assert decision.model == "haiku"


def test_signals_recorded_in_audit_trail() -> None:
    """Matched patterns are surfaced so the audit log can replay them."""
    decision = classify_failure("ESLint: 'foo' is not defined")
    assert decision.kind == "config"
    assert any("eslint" in sig for sig in decision.matched_signals)
