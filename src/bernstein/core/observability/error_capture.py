"""Shared routing helper for genuine, unexpected failure signals.

Bernstein's error sink (any Sentry-protocol backend behind the
single portable ``BERNSTEIN_TELEMETRY_DSN`` contract) only ever sees an
event when something explicitly forwards one. The CLI's top-level barrier
already does this for unhandled command exceptions
(:mod:`bernstein.cli.first_run_guard`). This module is the counterpart for
failures that surface *below* the CLI barrier and inside worker
subprocesses: a task exhausting its retries into the dead-letter queue, an
agent process crashing, the autofix daemon faulting on a repo.

Two transports share the one DSN contract, and this helper fans out to
both:

* **sentry-sdk** -- the idiomatic ``sentry_sdk.get_client().is_active()``
  guard from PR #1762. When the SDK has been initialised (the CLI ran
  :func:`bernstein.cli.main._init_error_telemetry`) the event gets the
  SDK's fingerprinting and release tag.
* **side channel** -- :mod:`bernstein.core.observability.sidechannel`, a
  dependency-free Sentry-store-protocol emitter over ``httpx``. It works
  in worker subprocesses that never ran the CLI's SDK ``init`` and in
  minimal installs without the ``observability`` extra, so the signal
  still lands. This is the same transport ``bernstein telemetry probe``
  uses, so it is known to reach the backend.

Both transports are best-effort and fail-closed: the call sites are
themselves failure paths, so nothing raised inside telemetry may ever
propagate back out and make a bad situation worse.

Only *unexpected* failures belong here. A task legitimately failing a
quality gate, a user-interrupt abort, or any other handled control-flow
outcome is not an error to report.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.observability import sidechannel

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOG = logging.getLogger(__name__)

__all__ = ["capture_exception", "capture_message"]


def _capture_via_sdk_exception(exc: BaseException) -> None:
    """Forward ``exc`` to ``sentry-sdk`` when the SDK is active. Never raises."""
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        client = sentry_sdk.get_client()
        if not client.is_active():
            return
        sentry_sdk.capture_exception(exc)
    except Exception as sdk_exc:  # pragma: no cover - defensive
        _LOG.debug("error_capture: sentry-sdk capture_exception failed: %s", sdk_exc)


def _capture_via_sdk_message(message: str, level: sidechannel.EventLevel) -> None:
    """Forward ``message`` to ``sentry-sdk`` when the SDK is active. Never raises."""
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        client = sentry_sdk.get_client()
        if not client.is_active():
            return
        sentry_sdk.capture_message(message, level=level.value)
    except Exception as sdk_exc:  # pragma: no cover - defensive
        _LOG.debug("error_capture: sentry-sdk capture_message failed: %s", sdk_exc)


def _mirror_to_sidechannel(
    *,
    category: str,
    message: str,
    level: sidechannel.EventLevel,
    tags: Mapping[str, str] | None,
    extra: Mapping[str, Any] | None,
) -> None:
    """Mirror the event onto the dependency-free side channel. Never raises."""
    try:
        sidechannel.emit(
            category,
            message,
            level=level,
            tags=dict(tags or {}),
            extra=dict(extra or {}),
        )
    except Exception as sc_exc:  # pragma: no cover - defensive
        _LOG.debug("error_capture: side-channel mirror failed: %s", sc_exc)


def capture_exception(
    exc: BaseException,
    *,
    category: str,
    tags: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Route an unexpected exception to the error sink, best-effort.

    Fans out to ``sentry-sdk`` (when initialised) and the side channel.
    The side-channel mirror carries ``category`` as the logger suffix so
    operators can filter one stream by failure surface in their
    error-tracker UI. Both transports are fail-closed: this never raises.

    Args:
        exc: The unexpected exception to report.
        category: Short failure-surface tag (e.g. ``"dead_letter"``,
            ``"agent"``, ``"autofix"``). Becomes ``logger=bernstein.<category>``.
        tags: Optional structured tags for backend search.
        extra: Optional structured context payload.
    """
    _capture_via_sdk_exception(exc)
    message = f"{type(exc).__name__}: {exc}"
    _mirror_to_sidechannel(
        category=category,
        message=message,
        level=sidechannel.EventLevel.ERROR,
        tags=tags,
        extra=extra,
    )


def capture_message(
    message: str,
    *,
    category: str,
    level: sidechannel.EventLevel = sidechannel.EventLevel.ERROR,
    tags: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Route an unexpected-failure message to the error sink, best-effort.

    Use this for terminal signals that are not themselves carried by a
    live exception object (e.g. a task reaching the dead-letter queue).
    Fans out to ``sentry-sdk`` (when initialised) and the side channel.
    Fail-closed: this never raises.

    Args:
        message: Human-readable failure description.
        category: Short failure-surface tag. Becomes
            ``logger=bernstein.<category>``.
        level: Severity. Defaults to :attr:`~sidechannel.EventLevel.ERROR`.
        tags: Optional structured tags for backend search.
        extra: Optional structured context payload.
    """
    _capture_via_sdk_message(message, level)
    _mirror_to_sidechannel(
        category=category,
        message=message,
        level=level,
        tags=tags,
        extra=extra,
    )
