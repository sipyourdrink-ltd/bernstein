"""Tests for AGENT-001 — categorized spawn failure errors."""

from __future__ import annotations

from bernstein.core.spawn_errors import (
    AdapterNotInstalledError,
    CategorizedSpawnError,
    ModelNotAvailableError,
    PermissionDeniedError,
    PromptTooLongError,
    ResourceExhaustedError,
    RetryStrategy,
    WorktreeCreationError,
    classify_spawn_error,
)

# ---------------------------------------------------------------------------
# Error types carry correct retry strategies
# ---------------------------------------------------------------------------


class TestRetryStrategies:
    def test_adapter_not_installed_no_retry(self) -> None:
        err = AdapterNotInstalledError("claude not found")
        assert err.retry_strategy == RetryStrategy.NO_RETRY

    def test_model_not_available_retry_fallback(self) -> None:
        err = ModelNotAvailableError("opus deprecated")
        assert err.retry_strategy == RetryStrategy.RETRY_FALLBACK

    def test_prompt_too_long_retry_fallback(self) -> None:
        err = PromptTooLongError("exceeds 200k tokens")
        assert err.retry_strategy == RetryStrategy.RETRY_FALLBACK

    def test_worktree_creation_retry_after_fix(self) -> None:
        err = WorktreeCreationError("stale lock")
        assert err.retry_strategy == RetryStrategy.RETRY_AFTER_FIX

    def test_permission_denied_retry_after_fix(self) -> None:
        err = PermissionDeniedError("invalid API key")
        assert err.retry_strategy == RetryStrategy.RETRY_AFTER_FIX

    def test_resource_exhausted_retry_same(self) -> None:
        err = ResourceExhaustedError("disk full")
        assert err.retry_strategy == RetryStrategy.RETRY_SAME

    def test_base_error_no_retry(self) -> None:
        err = CategorizedSpawnError("unknown error")
        assert err.retry_strategy == RetryStrategy.NO_RETRY


# ---------------------------------------------------------------------------
# Provider and detail metadata
# ---------------------------------------------------------------------------


class TestErrorMetadata:
    def test_provider_stored(self) -> None:
        err = ModelNotAvailableError("nope", provider="anthropic")
        assert err.provider == "anthropic"

    def test_detail_stored(self) -> None:
        err = PermissionDeniedError("bad key", detail="HTTP 403")
        assert err.detail == "HTTP 403"

    def test_to_dict(self) -> None:
        err = ResourceExhaustedError("disk full", provider="openai", detail="ENOSPC")
        d = err.to_dict()
        assert d["error_type"] == "ResourceExhaustedError"
        assert d["retry_strategy"] == "retry_same"
        assert d["provider"] == "openai"
        assert d["detail"] == "ENOSPC"
        assert "disk full" in d["message"]


# ---------------------------------------------------------------------------
# classify_spawn_error
# ---------------------------------------------------------------------------


class TestClassifySpawnError:
    def test_passthrough_categorized(self) -> None:
        original = AdapterNotInstalledError("not found")
        classified = classify_spawn_error(original)
        assert classified is original

    def test_command_not_found(self) -> None:
        raw = FileNotFoundError("No such file or directory: 'claude'")
        classified = classify_spawn_error(raw, provider="anthropic")
        assert isinstance(classified, AdapterNotInstalledError)
        assert classified.provider == "anthropic"

    def test_model_not_available(self) -> None:
        raw = RuntimeError("Model opus-3 not available")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, ModelNotAvailableError)

    def test_prompt_too_long(self) -> None:
        raw = ValueError("Prompt too long, exceeds context")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, PromptTooLongError)

    def test_worktree_error(self) -> None:
        raw = RuntimeError("git worktree add failed")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, WorktreeCreationError)

    def test_permission_denied(self) -> None:
        raw = PermissionError("Permission denied: /var/run/agent.sock")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, PermissionDeniedError)

    def test_disk_full(self) -> None:
        raw = OSError("No space left on device")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, ResourceExhaustedError)

    def test_oom(self) -> None:
        raw = MemoryError("Out of memory")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, ResourceExhaustedError)

    def test_unknown_error(self) -> None:
        raw = RuntimeError("something completely unknown happened")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, CategorizedSpawnError)
        assert classified.retry_strategy == RetryStrategy.NO_RETRY

    def test_forbidden_maps_to_permission(self) -> None:
        raw = RuntimeError("HTTP 403 Forbidden")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, PermissionDeniedError)

    def test_too_many_open_files(self) -> None:
        raw = OSError("Too many open files")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, ResourceExhaustedError)
