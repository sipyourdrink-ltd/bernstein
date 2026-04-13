"""Tests for ORCH-001: Error classifier for spawner exceptions."""

from __future__ import annotations

import pytest
from bernstein.core.error_classifier import (
    ClassifiedError,
    ErrorClassifier,
    FailureCategory,
)


@pytest.fixture
def classifier() -> ErrorClassifier:
    """Return a fresh ErrorClassifier instance."""
    return ErrorClassifier()


# ---------------------------------------------------------------------------
# FailureCategory enum
# ---------------------------------------------------------------------------


class TestFailureCategory:
    """Tests for the FailureCategory enum."""

    def test_all_categories_exist(self) -> None:
        assert FailureCategory.TRANSIENT == "transient"
        assert FailureCategory.PERMANENT == "permanent"
        assert FailureCategory.RESOURCE_EXHAUSTION == "resource_exhaustion"
        assert FailureCategory.CONFIG_ERROR == "config_error"

    def test_four_categories(self) -> None:
        assert len(FailureCategory) == 4


# ---------------------------------------------------------------------------
# Type-based classification
# ---------------------------------------------------------------------------


class TestTypeClassification:
    """Tests for exception type-based classification."""

    def test_connection_error_is_transient(self, classifier: ErrorClassifier) -> None:
        exc = ConnectionError("connection refused")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.TRANSIENT
        assert result.retryable is True

    def test_timeout_error_is_transient(self, classifier: ErrorClassifier) -> None:
        exc = TimeoutError("read timed out")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.TRANSIENT
        assert result.retryable is True

    def test_permission_error_is_config(self, classifier: ErrorClassifier) -> None:
        exc = PermissionError("access denied")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.CONFIG_ERROR
        assert result.retryable is False

    def test_memory_error_is_resource(self, classifier: ErrorClassifier) -> None:
        exc = MemoryError("out of memory")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.RESOURCE_EXHAUSTION
        assert result.retryable is False

    def test_file_not_found_is_config(self, classifier: ErrorClassifier) -> None:
        exc = FileNotFoundError("adapter not found")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.CONFIG_ERROR
        assert result.retryable is False

    def test_rate_limit_error_is_transient(self, classifier: ErrorClassifier) -> None:
        from bernstein.adapters.base import RateLimitError

        exc = RateLimitError("429 Too Many Requests")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.TRANSIENT
        assert result.retryable is True


# ---------------------------------------------------------------------------
# Message pattern classification
# ---------------------------------------------------------------------------


class TestMessageClassification:
    """Tests for exception message pattern-based classification."""

    @pytest.mark.parametrize(
        "msg",
        [
            "HTTP 429 rate limit exceeded",
            "503 Service Unavailable",
            "502 Bad Gateway from upstream",
            "504 Gateway Timeout",
            "Server overloaded, try again",
            "Connection timed out after 30s",
            "Connection reset by peer",
            "ECONNRESET",
            "Network unreachable",
        ],
    )
    def test_transient_patterns(self, classifier: ErrorClassifier, msg: str) -> None:
        result = classifier.classify(RuntimeError(msg))
        assert result.category == FailureCategory.TRANSIENT
        assert result.retryable is True

    @pytest.mark.parametrize(
        "msg",
        [
            "Out of memory allocating buffer",
            "OOM killed",
            "Token budget exceeded: 100000 tokens",
            "Context length exceeded: 200k tokens",
            "Disk full: no space left",
            "Quota exceeded for project",
            "HTTP 413 Request Entity Too Large",
        ],
    )
    def test_resource_patterns(self, classifier: ErrorClassifier, msg: str) -> None:
        result = classifier.classify(RuntimeError(msg))
        assert result.category == FailureCategory.RESOURCE_EXHAUSTION
        assert result.retryable is False

    @pytest.mark.parametrize(
        "msg",
        [
            "API key missing for provider",
            "HTTP 401 Unauthorized",
            "HTTP 403 Forbidden",
            "Invalid model: gpt-99",
            "Model not found: claude-4",
            "Authentication failed for endpoint",
            "Not authorized to access this resource",
        ],
    )
    def test_config_patterns(self, classifier: ErrorClassifier, msg: str) -> None:
        result = classifier.classify(RuntimeError(msg))
        assert result.category == FailureCategory.CONFIG_ERROR
        assert result.retryable is False

    @pytest.mark.parametrize(
        "msg",
        [
            "HTTP 400 Bad Request",
            "Invalid request: missing field 'prompt'",
            "Malformed JSON in request body",
            "Unsupported parameter: stream=True",
        ],
    )
    def test_permanent_patterns(self, classifier: ErrorClassifier, msg: str) -> None:
        result = classifier.classify(RuntimeError(msg))
        assert result.category == FailureCategory.PERMANENT
        assert result.retryable is False


# ---------------------------------------------------------------------------
# Chained exception classification
# ---------------------------------------------------------------------------


class TestChainedExceptions:
    """Tests for chained exception handling."""

    def test_chained_cause_is_checked(self, classifier: ErrorClassifier) -> None:
        cause = RuntimeError("HTTP 429 rate limited")
        exc = RuntimeError("spawn failed")
        exc.__cause__ = cause
        result = classifier.classify(exc)
        assert result.category == FailureCategory.TRANSIENT

    def test_chained_context_is_checked(self, classifier: ErrorClassifier) -> None:
        context = RuntimeError("Connection refused")
        exc = RuntimeError("unknown error during fetch")
        exc.__context__ = context
        result = classifier.classify(exc)
        assert result.category == FailureCategory.TRANSIENT


# ---------------------------------------------------------------------------
# Default classification
# ---------------------------------------------------------------------------


class TestDefaultClassification:
    """Tests for unclassified exceptions."""

    def test_unknown_exception_defaults_to_transient(self, classifier: ErrorClassifier) -> None:
        exc = RuntimeError("something completely unexpected")
        result = classifier.classify(exc)
        assert result.category == FailureCategory.TRANSIENT
        assert result.retryable is True
        assert "Unclassified" in result.reason


# ---------------------------------------------------------------------------
# ClassifiedError dataclass
# ---------------------------------------------------------------------------


class TestClassifiedError:
    """Tests for the ClassifiedError dataclass."""

    def test_fields_populated(self, classifier: ErrorClassifier) -> None:
        exc = ConnectionError("refused")
        result = classifier.classify(exc)
        assert isinstance(result, ClassifiedError)
        assert result.original is exc
        assert result.category == FailureCategory.TRANSIENT
        assert result.retryable is True
        assert len(result.reason) > 0
