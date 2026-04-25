"""Port detection helpers for ``bernstein preview``.

Two responsibilities:

* :func:`capture_port` walks one or more lines of dev-server stdout and
  returns the first port number matched by the configured regexes.
* :func:`probe_port` performs a TCP probe against ``localhost:<port>``
  with a configurable timeout — the gate the preview manager uses to
  decide whether to open a tunnel.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


#: Regexes applied to dev-server stdout in declaration order. The first
#: match wins. Tuned for the most common frameworks (Vite, Next.js,
#: webpack-dev-server, Vercel, Rails, Django runserver, Flask, generic
#: ``listening on``-style logs).
PORT_REGEX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"localhost:(\d{2,5})\b"),
    re.compile(r"127\.0\.0\.1:(\d{2,5})\b"),
    re.compile(r"\b0\.0\.0\.0:(\d{2,5})\b"),
    re.compile(r"Listening on (?:port )?(\d{2,5})\b", re.IGNORECASE),
    re.compile(r"Local:\s+https?://[^:]+:(\d{2,5})", re.IGNORECASE),
    re.compile(r"port[:=]\s*(\d{2,5})", re.IGNORECASE),
)


class PortNotDetectedError(RuntimeError):
    """Raised when no candidate port could be parsed from dev-server output."""


def _is_valid_port(value: int) -> bool:
    """Return ``True`` for ports in the unprivileged-or-bound range."""
    return 1 <= value <= 65535


def capture_port(
    lines: Iterable[str],
    *,
    patterns: tuple[re.Pattern[str], ...] | None = None,
) -> int | None:
    """Walk *lines* and return the first port number matched.

    Args:
        lines: Iterable of stdout lines (no trailing newline assumed).
        patterns: Optional override of the regex tuple. Defaults to
            :data:`PORT_REGEX_PATTERNS`.

    Returns:
        The matched port as an integer, or ``None`` if no line matched
        any pattern.
    """
    candidates = patterns or PORT_REGEX_PATTERNS
    for line in lines:
        for pattern in candidates:
            match = pattern.search(line)
            if match is None:
                continue
            try:
                port = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if _is_valid_port(port):
                return port
    return None


def probe_port(
    port: int,
    *,
    host: str = "127.0.0.1",
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
    sleeper: object | None = None,
    clock: object | None = None,
) -> bool:
    """Wait until ``host:port`` accepts a TCP connection or *timeout* elapses.

    The function is the green-light gate before a tunnel is opened. It
    keeps trying ``connect_ex`` until either a connection succeeds or
    *timeout_seconds* elapses on a monotonic clock.

    Args:
        port: TCP port to probe.
        host: Hostname / IP to dial. Defaults to ``127.0.0.1``.
        timeout_seconds: Wall-clock budget in seconds. Default: 30.
        poll_interval_seconds: Delay between connect attempts. Smaller
            values converge faster but burn more CPU; default 250 ms.
        sleeper: Optional callable used in place of :func:`time.sleep`.
            Hook for tests.
        clock: Optional callable used in place of :func:`time.monotonic`.
            Hook for tests.

    Returns:
        ``True`` once a TCP connection succeeded; ``False`` if the
        budget expired without a successful connect.
    """
    if not _is_valid_port(port):
        return False
    monotonic = clock if callable(clock) else time.monotonic
    sleep = sleeper if callable(sleeper) else time.sleep
    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError as exc:
            logger.debug("probe %s:%d failed: %s", host, port, exc)
        now = monotonic()
        if now >= deadline:
            return False
        # Sleep but never overshoot the deadline.
        remaining = deadline - now
        sleep(min(poll_interval_seconds, max(0.0, remaining)))


__all__ = [
    "PORT_REGEX_PATTERNS",
    "PortNotDetectedError",
    "capture_port",
    "probe_port",
]
