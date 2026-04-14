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


_SPAWN_ERROR_PATTERNS: list[tuple[type[CategorizedSpawnError], str, tuple[str, ...]]] = [
    (AdapterNotInstalledError, "Adapter binary not found", ("not found", "no such file", "command not found")),
    (WorktreeCreationError, "Worktree creation failed", ("worktree", "git worktree")),
    (RateLimitError, "Rate limit exceeded", ("rate limit", "ratelimit", "too many requests", "429")),
    (PermissionDeniedError, "Permission denied", ("permission", "denied", "unauthorized", "forbidden")),
    (
        ResourceExhaustedError,
        "Resource exhausted",
        ("disk", "no space", "out of memory", "oom", "resource", "too many open files"),
    ),
]


def _match_model_error(msg: str) -> bool:
    """Check if the error message indicates a model availability issue."""
    return "model" in msg and any(kw in msg for kw in ("not available", "not found", "deprecated"))


def _match_prompt_error(msg: str) -> bool:
    """Check if the error message indicates a prompt size issue."""
    return "prompt" in msg and any(kw in msg for kw in ("too long", "too large", "exceed"))


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
    if isinstance(exc, CategorizedSpawnError):
        return exc

    msg = str(exc).lower()
    detail = str(exc)

    # Check compound patterns first (require multiple keywords)
    if _match_model_error(msg):
        return ModelNotAvailableError(f"Model not available: {exc}", provider=provider, detail=detail)
    if _match_prompt_error(msg):
        return PromptTooLongError(f"Prompt too long: {exc}", provider=provider, detail=detail)

    # Check simple keyword patterns
    for error_cls, label, keywords in _SPAWN_ERROR_PATTERNS:
        if any(kw in msg for kw in keywords):
            return error_cls(f"{label}: {exc}", provider=provider, detail=detail)

    return CategorizedSpawnError(detail, provider=provider, detail=detail)
