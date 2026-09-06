"""Wire-in helpers for opt-in operator observability.

These helpers are the only place where the rest of the codebase touches
the telemetry boundary.  All entry points are fail-closed: if anything
inside the helper raises, the caller never sees it.

Three points integrate with the rest of Bernstein:

* ``emit_first_run_started`` / ``emit_first_run_completed``  - from
  ``bernstein.cli.run_bootstrap`` around the operator's first command.
* ``emit_command_invoked``  - from ``bernstein.core.worker`` at spawn
  time, with the bare command name only.
* ``maybe_print_first_run_notice``  - from the CLI bootstrap to print
  the one-time notice and persist the acknowledgement marker.
"""

from __future__ import annotations

import logging
import platform
import sys
import time
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.telemetry import config as cfg
from bernstein.core.telemetry.client import Client, get_client

if TYPE_CHECKING:
    from pathlib import Path
from bernstein.core.telemetry.events import (
    CommandInvokedPayload,
    DailyActivePayload,
    ErrorCategory,
    FirstRunCompletedPayload,
    FirstRunStartedPayload,
    InstallCompletedPayload,
    TelemetryEvent,
)

_LOG = logging.getLogger(__name__)


FIRST_RUN_NOTICE: Final[str] = (
    "Bernstein collects no telemetry by default.\n"
    "Run `bernstein telemetry on` to opt in.\n"
    "This message will not appear again."
)


def maybe_print_first_run_notice(
    *,
    home: Path | None = None,
    out: Any = None,
) -> bool:
    """Print the first-run notice exactly once.

    Returns ``True`` if the notice was printed, ``False`` otherwise.
    Failures inside the helper never raise.
    """
    try:
        if cfg.is_first_run_acknowledged(home=home):
            return False
        message = FIRST_RUN_NOTICE
        if out is None:
            print(message, file=sys.stderr)
        else:
            try:
                out.write(message + "\n")
            except Exception:
                print(message, file=sys.stderr)
        cfg.mark_first_run_acknowledged(home=home)
        return True
    except Exception as exc:
        _LOG.debug("telemetry: first-run notice failed (suppressed): %s", exc)
        return False


def _bernstein_version() -> str:
    """Best-effort version lookup; never raises."""
    try:
        from importlib.metadata import version

        return version("bernstein")
    except Exception:
        return "unknown"


def emit_install_completed(
    *,
    install_method: str = "pip",
    client: Client | None = None,
) -> None:
    """Emit a single install_completed event.  No-op when opted out."""
    _safe_emit(
        client,
        TelemetryEvent.INSTALL_COMPLETED,
        InstallCompletedPayload(
            os=platform.system().lower(),
            py_version=platform.python_version(),
            install_method=install_method,
            bernstein_version=_bernstein_version(),
        ),
    )


def emit_first_run_started(
    *,
    time_since_install_seconds: int,
    client: Client | None = None,
) -> None:
    """Emit first_run_started.  No-op when opted out."""
    _safe_emit(
        client,
        TelemetryEvent.FIRST_RUN_STARTED,
        FirstRunStartedPayload(
            time_since_install_seconds=max(0, time_since_install_seconds),
        ),
    )


def emit_first_run_completed(
    *,
    ok: bool,
    duration_ms: int,
    error_category: ErrorCategory | None = None,
    client: Client | None = None,
) -> None:
    """Emit first_run_completed.  No-op when opted out."""
    if not ok and error_category is None:
        error_category = ErrorCategory.UNKNOWN
    if ok:
        error_category = None
    _safe_emit(
        client,
        TelemetryEvent.FIRST_RUN_COMPLETED,
        FirstRunCompletedPayload(
            ok=ok,
            duration_ms=max(0, duration_ms),
            error_category=error_category,
        ),
    )


def emit_command_invoked(
    *,
    name_only: str,
    client: Client | None = None,
) -> None:
    """Emit command_invoked.  ``name_only`` must be the command name only."""
    # Defensive: strip anything that looks like an arg or a path separator.
    clean = (name_only).strip().split()[0] if name_only else ""
    clean = clean.replace("/", "_").replace("\\", "_")
    if not clean:
        return
    _safe_emit(
        client,
        TelemetryEvent.COMMAND_INVOKED,
        CommandInvokedPayload(
            name_only=clean,
            bernstein_version=_bernstein_version(),
        ),
    )


def emit_daily_active(
    *,
    day_iso: str,
    client: Client | None = None,
) -> None:
    """Emit daily_active.  Caller deduplicates to at most one per day."""
    _safe_emit(
        client,
        TelemetryEvent.DAILY_ACTIVE,
        DailyActivePayload(day_iso=day_iso),
    )


class FirstRunTimer:
    """Context manager that emits start/completed pairs.

    Designed for use at the top of ``bernstein run``::

        with FirstRunTimer() as timer:
            ...
            timer.set_error(ErrorCategory.AUTH_FAILED)
    """

    def __init__(
        self,
        *,
        time_since_install_seconds: int = 0,
        client: Client | None = None,
    ) -> None:
        self._client = client
        self._time_since_install = time_since_install_seconds
        self._started_at: float = 0.0
        self._error: ErrorCategory | None = None

    def __enter__(self) -> FirstRunTimer:
        self._started_at = time.monotonic()
        emit_first_run_started(
            time_since_install_seconds=self._time_since_install,
            client=self._client,
        )
        return self

    def set_error(self, category: ErrorCategory) -> None:
        """Record an error category to emit on exit."""
        self._error = category

    def __exit__(
        self,
        _exc_type: object,
        exc: object,
        _tb: object,
    ) -> None:
        duration_ms = int((time.monotonic() - self._started_at) * 1000)
        if exc is not None and self._error is None:
            self._error = ErrorCategory.UNKNOWN
        emit_first_run_completed(
            ok=self._error is None,
            duration_ms=duration_ms,
            error_category=self._error,
            client=self._client,
        )


def _safe_emit(
    client: Client | None,
    name: TelemetryEvent,
    payload: object,
) -> None:
    """Inner shared boundary used by every emit_* helper."""
    try:
        c = client if client is not None else get_client()
        c.emit(name, payload)  # type: ignore[arg-type]  # dynamic payload
    except Exception as exc:
        _LOG.debug("telemetry: wire emit failed (suppressed): %s", exc)


__all__ = [
    "FIRST_RUN_NOTICE",
    "FirstRunTimer",
    "emit_command_invoked",
    "emit_daily_active",
    "emit_first_run_completed",
    "emit_first_run_started",
    "emit_install_completed",
    "maybe_print_first_run_notice",
]
