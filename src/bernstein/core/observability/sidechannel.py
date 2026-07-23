"""Portable side-channel telemetry for Bernstein.

When Bernstein runs embedded inside a host application (Claude Desktop,
Cursor, and similar) it cannot rely on the host's stdout to surface what
the agents did: the host neither intercepts nor forwards Bernstein's own
observability stream. This module ships that stream over a side channel
the operator controls, with a single contract that is identical across
every host:

* One environment variable everywhere: ``BERNSTEIN_TELEMETRY_DSN``.
* One wire format: the Sentry store protocol.
* One backend contract: any Sentry-compatible sink behind a DSN.

The full contract is documented in ``docs/observability/side-channel.md``.

Design constraints (mirrors the rest of the observability boundary):

* Default state is off. With no DSN configured, :func:`get_sidechannel`
  returns a sink that drops every event and touches no network.
* The boundary is fail-closed: no exception raised inside the sink ever
  propagates to the caller. A misconfigured DSN must not crash a run.
* Backpressure is explicit. The sink has a bounded in-memory queue; when
  it is full the configured policy decides between dropping the newest
  event (``drop``, the default) or blocking the producer until a slot
  frees up (``queue``).

The module is intentionally transport-light: it speaks the Sentry store
protocol directly over ``httpx`` rather than depending on ``sentry-sdk``,
so a minimal install without the ``observability`` extra still emits.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOG = logging.getLogger(__name__)

#: Portable env var carrying the Sentry-compatible DSN. The same name is
#: honoured regardless of which host launched Bernstein.
DSN_ENV: Final[str] = "BERNSTEIN_TELEMETRY_DSN"

#: Env var selecting the backpressure policy when the queue is full.
#: ``drop`` (default) discards the newest event; ``queue`` blocks the
#: producer until a slot frees up (bounded by :data:`QUEUE_BLOCK_SECONDS`).
BACKPRESSURE_ENV: Final[str] = "BERNSTEIN_TELEMETRY_BACKPRESSURE"

#: Env var overriding the bounded queue depth.
QUEUE_MAXSIZE_ENV: Final[str] = "BERNSTEIN_TELEMETRY_QUEUE_MAXSIZE"

#: Default queue depth. Sized so a burst of lifecycle events does not block
#: the orchestrator under the default ``drop`` policy.
DEFAULT_QUEUE_MAXSIZE: Final[int] = 256

#: Maximum time the ``queue`` policy will block a producer before giving up
#: and dropping the event. Keeps a stalled backend from wedging a run.
QUEUE_BLOCK_SECONDS: Final[float] = 1.0

#: Per-request HTTP timeout for the Sentry store endpoint.
TIMEOUT_SECONDS: Final[float] = 5.0

#: Bound on the background worker join during shutdown flush.
FLUSH_DEADLINE_SECONDS: Final[float] = 5.0

#: Sentry client identifier sent in the auth header. Operators see this in
#: their error-tracker UI so they can tell Bernstein-shipped events apart.
SENTRY_CLIENT: Final[str] = "bernstein-sidechannel/1"

#: Sentry store-protocol version. ``7`` is the long-standing default that
#: Sentry-compatible backends accept.
SENTRY_PROTOCOL_VERSION: Final[str] = "7"

#: Maximum number of events kept in the offline preview ring buffer.
#: This buffer powers ``bernstein telemetry tail`` so operators can audit
#: the stream offline before any network send. It is intentionally small.
PREVIEW_BUFFER_MAXSIZE: Final[int] = 128


_preview_buffer: deque[dict[str, Any]] = deque(maxlen=PREVIEW_BUFFER_MAXSIZE)
_preview_lock = threading.Lock()


def record_preview(payload: Mapping[str, Any]) -> None:
    """Append a rendered event payload to the offline preview ring buffer.

    The buffer is bounded; oldest entries are evicted automatically. It is
    intentionally a shallow record of what ``emit`` produced before any
    network attempt, so operators can audit the stream offline regardless
    of whether the backend is reachable.
    """
    with _preview_lock:
        _preview_buffer.append(dict(payload))


def read_preview(n: int = 10) -> list[dict[str, Any]]:
    """Return the most recent ``n`` rendered events from the preview buffer.

    The returned list is ordered oldest-first. ``n`` is clamped to the
    buffer's configured capacity.
    """
    if n <= 0:
        return []
    with _preview_lock:
        snapshot = list(_preview_buffer)
    if n >= len(snapshot):
        return snapshot
    return snapshot[-n:]


def clear_preview() -> None:
    """Drop the preview buffer. Used by tests."""
    with _preview_lock:
        _preview_buffer.clear()


class Backpressure(StrEnum):
    """Behaviour when the bounded queue is full."""

    DROP = "drop"
    QUEUE = "queue"


class EventLevel(StrEnum):
    """Sentry severity levels accepted by the store protocol."""

    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    DEBUG = "debug"


# ---------------------------------------------------------------------------
# DSN parsing
# ---------------------------------------------------------------------------


class DsnError(ValueError):
    """Raised when ``BERNSTEIN_TELEMETRY_DSN`` cannot be parsed."""


@dataclass(frozen=True, slots=True)
class Dsn:
    """A parsed Sentry-compatible DSN.

    A DSN has the shape ``<scheme>://<public_key>@<host>[:<port>]/<project_id>``.
    The store endpoint derived from it is
    ``<scheme>://<host>[:<port>]/api/<project_id>/store/``.
    """

    scheme: str
    public_key: str
    host: str
    project_id: str
    port: int | None = None

    @property
    def netloc(self) -> str:
        """Return ``host`` or ``host:port`` for URL composition."""
        if self.port is None:
            return self.host
        return f"{self.host}:{self.port}"

    @property
    def store_url(self) -> str:
        """Return the Sentry store endpoint for this DSN."""
        return f"{self.scheme}://{self.netloc}/api/{self.project_id}/store/"

    def auth_header(self, *, now: float | None = None) -> str:
        """Return the ``X-Sentry-Auth`` header value for this DSN."""
        ts = now if now is not None else time.time()
        parts = [
            f"sentry_version={SENTRY_PROTOCOL_VERSION}",
            f"sentry_client={SENTRY_CLIENT}",
            f"sentry_timestamp={ts:.0f}",
            f"sentry_key={self.public_key}",
        ]
        return "Sentry " + ", ".join(parts)


def parse_dsn(raw: str) -> Dsn:
    """Parse a Sentry-compatible DSN string.

    Raises:
        DsnError: if the DSN is empty, has an unsupported scheme, or is
            missing the public key, host, or project id.
    """
    if not raw or not raw.strip():
        raise DsnError("DSN is empty")
    parsed = urlparse(raw.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise DsnError(f"DSN scheme must be http or https, got {scheme!r}")
    if not parsed.username:
        raise DsnError("DSN is missing the public key (user component)")
    if not parsed.hostname:
        raise DsnError("DSN is missing the host")
    project_id = parsed.path.strip("/")
    if not project_id:
        raise DsnError("DSN is missing the project id (path component)")
    return Dsn(
        scheme=scheme,
        public_key=parsed.username,
        host=parsed.hostname,
        project_id=project_id,
        port=parsed.port,
    )


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SideChannelEvent:
    """A single side-channel event.

    Required fields per the contract: ``category``, ``message``,
    ``level``. ``tags`` and ``extra`` are optional structured context.
    The ``event_id`` and ``timestamp`` are filled in automatically when
    omitted so callers only supply the meaningful fields.
    """

    category: str
    message: str
    level: EventLevel = EventLevel.INFO
    tags: dict[str, str] = field(default_factory=dict[str, str])
    extra: dict[str, Any] = field(default_factory=dict[str, Any])
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())

    def to_payload(self) -> dict[str, Any]:
        """Render the Sentry store-protocol body for this event.

        ``logger`` carries the emitter category (``lineage``, ``cost``,
        ``run``, ``tracker``, ...) so operators can filter one stream by
        source in their error-tracker UI. The ``bernstein.category`` tag carries
        the same value for tag-based search.
        """
        tags = self.tags.copy()
        tags.setdefault("bernstein.category", self.category)
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "platform": "python",
            "level": self.level.value,
            "logger": f"bernstein.{self.category}",
            "message": self.message,
            "tags": tags,
            "extra": self.extra.copy(),
            "sdk": {"name": "bernstein.sidechannel", "version": "1"},
        }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class _HttpTransport:
    """Posts a single rendered event to the Sentry store endpoint.

    Kept behind a tiny interface so tests can inject a fake without any
    network. Failures are surfaced as ``False`` rather than raised; the
    sink translates that into a dropped event and a debug log.
    """

    def __init__(self, dsn: Dsn) -> None:
        self._dsn = dsn

    def send(self, payload: Mapping[str, Any]) -> bool:
        try:
            import httpx
        except ImportError:
            _LOG.debug("sidechannel: httpx not installed; event dropped")
            return False
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Sentry-Auth": self._dsn.auth_header(),
        }
        try:
            resp = httpx.post(
                self._dsn.store_url,
                content=body,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:
            _LOG.debug("sidechannel: POST failed (suppressed): %s", exc)
            return False
        ok = 200 <= resp.status_code < 300
        if not ok:
            _LOG.debug("sidechannel: store returned HTTP %s", resp.status_code)
        return ok


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class NullSideChannel:
    """Sink used when no DSN is configured. Drops every event."""

    enabled: bool = False

    def emit(self, event: SideChannelEvent) -> bool:
        return False

    def flush(self, deadline_seconds: float = FLUSH_DEADLINE_SECONDS) -> None:
        _ = deadline_seconds
        return None

    def close(self) -> None:
        return None


class SideChannel:
    """Bounded, fail-closed side-channel sink.

    Events are rendered and handed to a background worker thread that
    posts them to the Sentry store endpoint. The hand-off is the only
    backpressure point: under ``drop`` a full queue discards the newest
    event; under ``queue`` the producer blocks for up to
    :data:`QUEUE_BLOCK_SECONDS` before giving up.
    """

    enabled: bool = True

    def __init__(
        self,
        dsn: Dsn,
        *,
        backpressure: Backpressure = Backpressure.DROP,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        transport: _HttpTransport | Any | None = None,
    ) -> None:
        self._dsn = dsn
        self._backpressure = backpressure
        self._transport = transport if transport is not None else _HttpTransport(dsn)
        self._queue: queue.Queue[SideChannelEvent | None] = queue.Queue(maxsize=max(1, maxsize))
        self._dropped = 0
        self._sent = 0
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run,
            name="bernstein-sidechannel",
            daemon=True,
        )
        self._worker.start()

    @property
    def dropped(self) -> int:
        """Number of events dropped under backpressure since construction."""
        with self._lock:
            return self._dropped

    @property
    def sent(self) -> int:
        """Number of events accepted by the transport since construction."""
        with self._lock:
            return self._sent

    def emit(self, event: SideChannelEvent) -> bool:
        """Enqueue ``event`` for delivery.

        Returns ``True`` when the event was accepted into the queue,
        ``False`` when it was dropped under backpressure. Never raises.
        """
        try:
            if self._backpressure is Backpressure.QUEUE:
                self._queue.put(event, block=True, timeout=QUEUE_BLOCK_SECONDS)
            else:
                self._queue.put_nowait(event)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            _LOG.debug("sidechannel: queue full; event dropped (%s)", event.category)
            return False
        except Exception as exc:
            _LOG.debug("sidechannel: emit failed (suppressed): %s", exc)
            return False

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._deliver(item)
            finally:
                self._queue.task_done()

    def _deliver(self, event: SideChannelEvent) -> None:
        try:
            ok = self._transport.send(event.to_payload())
        except Exception as exc:
            _LOG.debug("sidechannel: delivery failed (suppressed): %s", exc)
            return
        if ok:
            with self._lock:
                self._sent += 1

    def flush(self, deadline_seconds: float = FLUSH_DEADLINE_SECONDS) -> None:
        """Best-effort drain of the queue, bounded by ``deadline_seconds``."""
        deadline = time.monotonic() + deadline_seconds
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        """Signal the worker to stop and join it within the flush deadline."""
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Best-effort: drop one queued event to make room for the stop
            # sentinel so close() never blocks forever.
            with contextlib.suppress(queue.Empty, queue.Full):
                self._queue.get_nowait()
                self._queue.put_nowait(None)
        self._worker.join(timeout=FLUSH_DEADLINE_SECONDS)


SideChannelSink = SideChannel | NullSideChannel


# ---------------------------------------------------------------------------
# Configuration resolution
# ---------------------------------------------------------------------------


def _resolve_backpressure(env: Mapping[str, str]) -> Backpressure:
    raw = (env.get(BACKPRESSURE_ENV) or "").strip().lower()
    if raw == Backpressure.QUEUE.value:
        return Backpressure.QUEUE
    return Backpressure.DROP


def _resolve_maxsize(env: Mapping[str, str]) -> int:
    raw = (env.get(QUEUE_MAXSIZE_ENV) or "").strip()
    if not raw:
        return DEFAULT_QUEUE_MAXSIZE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_QUEUE_MAXSIZE
    return value if value > 0 else DEFAULT_QUEUE_MAXSIZE


def build_sidechannel(
    *,
    env: Mapping[str, str] | None = None,
    transport: _HttpTransport | Any | None = None,
) -> SideChannelSink:
    """Build a side-channel sink from the environment.

    Returns a :class:`NullSideChannel` when no DSN is configured or the
    DSN cannot be parsed, so the boundary is fail-closed at construction
    time. Otherwise returns a live :class:`SideChannel`.
    """
    real_env = env if env is not None else os.environ
    raw = real_env.get(DSN_ENV)
    if not raw:
        return NullSideChannel()
    try:
        dsn = parse_dsn(raw)
    except DsnError as exc:
        _LOG.warning("sidechannel: invalid %s, telemetry disabled: %s", DSN_ENV, exc)
        return NullSideChannel()
    return SideChannel(
        dsn,
        backpressure=_resolve_backpressure(real_env),
        maxsize=_resolve_maxsize(real_env),
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------

_default_sink: SideChannelSink | None = None
_default_lock = threading.Lock()


def get_sidechannel() -> SideChannelSink:
    """Return the process-wide default side-channel sink."""
    global _default_sink
    with _default_lock:
        if _default_sink is None:
            _default_sink = build_sidechannel()
        return _default_sink


def reset_sidechannel() -> None:
    """Drop the cached default sink. Used by tests and reconfiguration."""
    global _default_sink
    with _default_lock:
        if _default_sink is not None:
            _default_sink.close()
        _default_sink = None


def emit(
    category: str,
    message: str,
    *,
    level: EventLevel = EventLevel.INFO,
    tags: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    sink: SideChannelSink | None = None,
) -> bool:
    """Emit a side-channel event through the default sink.

    This is the single entry point existing emitters route through. It is
    fail-closed: any error is swallowed and reported as ``False``.
    """
    try:
        target = sink if sink is not None else get_sidechannel()
        event = SideChannelEvent(
            category=category,
            message=message,
            level=level,
            tags=dict(tags or {}),
            extra=dict(extra or {}),
        )
        # Record the rendered payload in the offline preview buffer so
        # ``bernstein telemetry tail`` can show what was queued for send,
        # whether or not the backend was reachable.
        with contextlib.suppress(Exception):
            record_preview(event.to_payload())
        return target.emit(event)
    except Exception as exc:
        _LOG.debug("sidechannel: emit helper failed (suppressed): %s", exc)
        return False


__all__ = [
    "BACKPRESSURE_ENV",
    "DEFAULT_QUEUE_MAXSIZE",
    "DSN_ENV",
    "FLUSH_DEADLINE_SECONDS",
    "PREVIEW_BUFFER_MAXSIZE",
    "QUEUE_MAXSIZE_ENV",
    "Backpressure",
    "Dsn",
    "DsnError",
    "EventLevel",
    "NullSideChannel",
    "SideChannel",
    "SideChannelEvent",
    "SideChannelSink",
    "build_sidechannel",
    "clear_preview",
    "emit",
    "get_sidechannel",
    "parse_dsn",
    "read_preview",
    "record_preview",
    "reset_sidechannel",
]
