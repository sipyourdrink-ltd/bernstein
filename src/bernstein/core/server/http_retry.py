"""Exponential backoff with jitter for HTTP calls.

Provides a ``retry_http`` decorator that wraps synchronous httpx calls
with configurable exponential backoff and full-jitter.  The decorator
handles transient HTTP errors (5xx, 429, connection errors) and retries
them with increasing delays.

The jitter strategy uses "full jitter" (uniform random between 0 and
the computed backoff) to decorrelate retries from multiple callers
hitting the same endpoint.

Usage::

    @retry_http(max_retries=3, base_delay_s=1.0, max_delay_s=30.0)
    def fetch_tasks(client: httpx.Client, url: str) -> httpx.Response:
        return client.get(url)
"""

from __future__ import annotations

import functools
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

logger = logging.getLogger(__name__)

F = TypeVar("F")

# HTTP status codes considered transient (retryable)
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504, 529})

# Exception types considered transient
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    ConnectionError,
    TimeoutError,
)


@dataclass(frozen=True)
class RetryConfig:
    """Configuration for HTTP retry behavior.

    Attributes:
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay_s: Base delay in seconds for the first retry.
        max_delay_s: Maximum delay cap in seconds.
        jitter: Whether to add full jitter to the delay.
        retryable_status_codes: HTTP status codes to retry on.
    """

    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True
    retryable_status_codes: frozenset[int] = _RETRYABLE_STATUS_CODES


def compute_backoff(
    attempt: int,
    base_delay_s: float,
    max_delay_s: float,
    *,
    jitter: bool = True,
) -> float:
    """Compute the backoff delay for a given retry attempt.

    Uses exponential backoff: ``base_delay * 2^attempt``, capped at
    ``max_delay_s``.  When ``jitter=True``, applies full jitter
    (uniform random in ``[0, computed_delay]``).

    Args:
        attempt: Zero-based retry attempt number.
        base_delay_s: Base delay in seconds.
        max_delay_s: Maximum delay cap in seconds.
        jitter: Whether to apply full jitter.

    Returns:
        Delay in seconds to sleep before the next attempt.
    """
    delay = min(base_delay_s * (2**attempt), max_delay_s)
    if jitter:
        delay = random.uniform(0, delay)
    return delay


def is_retryable_response(response: httpx.Response, config: RetryConfig) -> bool:
    """Check if an HTTP response indicates a retryable error.

    Args:
        response: The httpx response.
        config: Retry configuration with retryable status codes.

    Returns:
        True if the response status code is retryable.
    """
    return response.status_code in config.retryable_status_codes


def is_retryable_exception(exc: Exception) -> bool:
    """Check if an exception is a retryable network/timeout error.

    Args:
        exc: The exception to check.

    Returns:
        True if the exception type is retryable.
    """
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def retry_http(
    max_retries: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    *,
    jitter: bool = True,
    retryable_status_codes: frozenset[int] | None = None,
) -> Any:
    """Decorator: retry a function that returns an httpx.Response.

    The decorated function is retried when it raises a retryable exception
    or returns a response with a retryable status code.  Between retries
    the decorator sleeps for an exponentially increasing duration with
    optional jitter.

    Args:
        max_retries: Maximum retry attempts.
        base_delay_s: Base delay for first retry.
        max_delay_s: Maximum delay cap.
        jitter: Whether to apply full jitter.
        retryable_status_codes: Override the default retryable status codes.

    Returns:
        Decorator that wraps the target function.
    """
    config = RetryConfig(
        max_retries=max_retries,
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
        jitter=jitter,
        retryable_status_codes=retryable_status_codes or _RETRYABLE_STATUS_CODES,
    )

    def decorator(func: Any) -> Any:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(config.max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    if not _should_retry_response(result, config, attempt):
                        return result
                    _backoff_and_log_response(result, func.__name__, attempt, config)
                    continue

                except Exception as exc:
                    last_exception = exc
                    if not is_retryable_exception(exc):
                        raise
                    if attempt >= config.max_retries:
                        raise
                    _backoff_and_log_exception(exc, func.__name__, attempt, config)

            if last_exception is not None:
                raise last_exception
            msg = f"Retry exhausted for {func.__name__}"
            raise RuntimeError(msg)

        return wrapper

    return decorator


def _should_retry_response(result: Any, config: RetryConfig, attempt: int) -> bool:
    """Check if a response should be retried."""
    return isinstance(result, httpx.Response) and is_retryable_response(result, config) and attempt < config.max_retries


def _backoff_and_log_response(result: Any, func_name: str, attempt: int, config: RetryConfig) -> None:
    """Log and sleep for a retryable HTTP response."""
    delay = compute_backoff(attempt, config.base_delay_s, config.max_delay_s, jitter=config.jitter)
    logger.warning(
        "Retryable HTTP %d from %s (attempt %d/%d), backing off %.2fs",
        result.status_code,
        func_name,
        attempt + 1,
        config.max_retries,
        delay,
    )
    time.sleep(delay)


def _backoff_and_log_exception(exc: Exception, func_name: str, attempt: int, config: RetryConfig) -> None:
    """Log and sleep for a retryable exception."""
    delay = compute_backoff(attempt, config.base_delay_s, config.max_delay_s, jitter=config.jitter)
    logger.warning(
        "Retryable error in %s (attempt %d/%d): %s. Backing off %.2fs",
        func_name,
        attempt + 1,
        config.max_retries,
        exc,
        delay,
    )
    time.sleep(delay)


def retry_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    config: RetryConfig | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP request with retry logic.

    A functional alternative to the decorator for ad-hoc calls.

    Args:
        client: httpx client instance.
        method: HTTP method (GET, POST, etc.).
        url: Request URL.
        config: Retry configuration (uses defaults if None).
        **kwargs: Additional arguments passed to ``client.request()``.

    Returns:
        The httpx response.

    Raises:
        httpx.HTTPError: If all retries are exhausted.
    """
    cfg = config or RetryConfig()
    last_exception: Exception | None = None

    for attempt in range(cfg.max_retries + 1):
        try:
            response = client.request(method, url, **kwargs)
            if is_retryable_response(response, cfg) and attempt < cfg.max_retries:
                delay = compute_backoff(attempt, cfg.base_delay_s, cfg.max_delay_s, jitter=cfg.jitter)
                logger.warning(
                    "Retryable HTTP %d from %s %s (attempt %d/%d), backing off %.2fs",
                    response.status_code,
                    method,
                    url,
                    attempt + 1,
                    cfg.max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            return response
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exception = exc
            if attempt < cfg.max_retries:
                delay = compute_backoff(attempt, cfg.base_delay_s, cfg.max_delay_s, jitter=cfg.jitter)
                logger.warning(
                    "Retryable error %s %s (attempt %d/%d): %s. Backing off %.2fs",
                    method,
                    url,
                    attempt + 1,
                    cfg.max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                raise

    # Should not reach here, but satisfy type checker
    if last_exception is not None:
        raise last_exception
    msg = f"Retry exhausted for {method} {url}"
    raise RuntimeError(msg)
