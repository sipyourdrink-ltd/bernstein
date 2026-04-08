"""Categorized spawn failure errors with retry strategy metadata (AGENT-001).

Each error type maps to a retry strategy that the spawner can use to decide
how to recover:

- ``RETRY_SAME``: Retry on the same provider (transient errors).
- ``RETRY_FALLBACK``: Switch to a fallback model/provider.
- ``NO_RETRY``: Permanent failure, do not retry.
- ``RETRY_AFTER_FIX``: Needs operator intervention, then retry.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RetryStrategy(StrEnum):
    """How the spawner should handle a categorized spawn failure."""

    RETRY_SAME = "retry_same"
    RETRY_FALLBACK = "retry_fallback"
    NO_RETRY = "no_retry"
    RETRY_AFTER_FIX = "retry_after_fix"


class CategorizedSpawnError(RuntimeError):
    """Base class for spawn errors with categorization and retry strategy.

    All subclasses carry a ``retry_strategy`` attribute so the spawner can
    route recovery logic without introspecting the error message.

    Attributes:
        retry_strategy: How the spawner should handle this error.
        provider: Provider that triggered the error, if known.
        detail: Additional detail (e.g. HTTP status, stderr snippet).
    """

    retry_strategy: RetryStrategy = RetryStrategy.NO_RETRY

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        """Serialize error metadata for logging/telemetry.

        Returns:
            Dict with error type, message, retry strategy, and provider.
        """
        return {
            "error_type": type(self).__name__,
            "message": str(self),
            "retry_strategy": self.retry_strategy.value,
            "provider": self.provider,
            "detail": self.detail,
        }


class AdapterNotInstalledError(CategorizedSpawnError):
    """The CLI adapter binary is not installed or not on PATH.

    Example: ``claude`` binary not found when using the Claude adapter.
    """

    retry_strategy = RetryStrategy.NO_RETRY


class ModelNotAvailableError(CategorizedSpawnError):
    """The requested model is not available on the selected provider.

    May be temporary (model deployment in progress) or permanent (model
    deprecated).  Default strategy is to try a fallback model.
    """

    retry_strategy = RetryStrategy.RETRY_FALLBACK


class PromptTooLongError(CategorizedSpawnError):
    """The rendered prompt exceeds the model's context window.

    The spawner should truncate the prompt or select a model with a larger
    context window.  Retrying with the same prompt will not help.
    """

    retry_strategy = RetryStrategy.RETRY_FALLBACK


class WorktreeCreationError(CategorizedSpawnError):
    """Git worktree creation failed.

    Common causes: stale locks, disk full, git index corruption.
    """

    retry_strategy = RetryStrategy.RETRY_AFTER_FIX


class PermissionDeniedError(CategorizedSpawnError):
    """The agent process was denied permission to run or access resources.

    Example: API key invalid, auth token expired, sandbox permission denied.
    """

    retry_strategy = RetryStrategy.RETRY_AFTER_FIX


class RateLimitError(CategorizedSpawnError):
    """Provider rate limit hit — try a different provider."""

    retry_strategy = RetryStrategy.RETRY_FALLBACK


class ResourceExhaustedError(CategorizedSpawnError):
    """System resources are exhausted (disk, memory, file descriptors, etc.).

    Retrying immediately will not help; wait for resources to free up.
    """

    retry_strategy = RetryStrategy.RETRY_SAME


def classify_spawn_error(
    exc: Exception,
    *,
    provider: str | None = None,
) -> CategorizedSpawnError:
    """Classify a raw exception into a categorized spawn error.

    Inspects the exception type and message to select the best matching
    error category.  Unknown errors are wrapped as the base
    ``CategorizedSpawnError``.

    Args:
        exc: The original exception from the spawn attempt.
        provider: Provider name for context, if known.

    Returns:
        A categorized spawn error wrapping the original exception.
    """
    msg = str(exc).lower()

    if isinstance(exc, CategorizedSpawnError):
        return exc

    if "not found" in msg or "no such file" in msg or "command not found" in msg:
        return AdapterNotInstalledError(
            f"Adapter binary not found: {exc}",
            provider=provider,
            detail=str(exc),
        )

    if "model" in msg and ("not available" in msg or "not found" in msg or "deprecated" in msg):
        return ModelNotAvailableError(
            f"Model not available: {exc}",
            provider=provider,
            detail=str(exc),
        )

    if "prompt" in msg and ("too long" in msg or "too large" in msg or "exceed" in msg):
        return PromptTooLongError(
            f"Prompt too long: {exc}",
            provider=provider,
            detail=str(exc),
        )

    if "worktree" in msg or "git worktree" in msg:
        return WorktreeCreationError(
            f"Worktree creation failed: {exc}",
            provider=provider,
            detail=str(exc),
        )

    if "rate limit" in msg or "ratelimit" in msg or "too many requests" in msg or "429" in msg:
        return RateLimitError(
            f"Rate limit exceeded: {exc}",
            provider=provider,
            detail=str(exc),
        )

    if "permission" in msg or "denied" in msg or "unauthorized" in msg or "forbidden" in msg:
        return PermissionDeniedError(
            f"Permission denied: {exc}",
            provider=provider,
            detail=str(exc),
        )

    if (
        "disk" in msg
        or "no space" in msg
        or "out of memory" in msg
        or "oom" in msg
        or "resource" in msg
        or "too many open files" in msg
    ):
        return ResourceExhaustedError(
            f"Resource exhausted: {exc}",
            provider=provider,
            detail=str(exc),
        )

    return CategorizedSpawnError(
        str(exc),
        provider=provider,
        detail=str(exc),
    )
