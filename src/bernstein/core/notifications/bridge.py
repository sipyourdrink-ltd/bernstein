"""Notification dispatcher: dedup, retry, dead-letter, audit.

The dispatcher sits between the lifecycle bridge and the per-sink
drivers. Its responsibilities:

  * **Dedup** — keep an in-memory LRU plus an on-disk window keyed by
    ``event_id`` so a restart-loop cannot spam a sink.
  * **Retry** — exponential backoff with configurable ``max_attempts``
    and ``initial_delay_ms``.
  * **Dead-letter** — append permanent failures to
    ``.sdd/runtime/notifications/dead_letter.jsonl`` with rotation.
  * **Audit** — every terminal outcome is appended to the HMAC chain
    (``event_id``, ``sink_id``, ``outcome``).

Drivers are not aware of any of this; they just implement the
:class:`~bernstein.core.notifications.protocol.NotificationSink`
protocol and raise the right error class for retry vs. permanent
failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationOutcome,
    NotificationPermanentError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from pathlib import Path

    from bernstein.core.notifications.protocol import NotificationSink

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_INITIAL_DELAY_MS: int = 250
DEFAULT_BACKOFF_FACTOR: float = 2.0
DEFAULT_DEDUP_LRU_SIZE: int = 2048
DEFAULT_DEDUP_WINDOW_SECONDS: int = 6 * 3600  # 6 hours
DEFAULT_DEAD_LETTER_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB before rotation

#: Audit event_type for delivery records appended to the HMAC chain.
AUDIT_EVENT_TYPE = "notification.delivery"

__all__ = [
    "AUDIT_EVENT_TYPE",
    "DEFAULT_DEAD_LETTER_MAX_BYTES",
    "DEFAULT_DEDUP_LRU_SIZE",
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "DEFAULT_INITIAL_DELAY_MS",
    "DEFAULT_MAX_ATTEMPTS",
    "DeadLetter",
    "DedupCache",
    "NotificationDispatcher",
    "RetryPolicy",
]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential-backoff retry knobs.

    Attributes:
        max_attempts: Total attempt count (>= 1). ``1`` disables retry.
        initial_delay_ms: First backoff sleep in milliseconds.
        backoff_factor: Multiplier applied after each failure.
        max_delay_ms: Cap on the per-attempt sleep.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_delay_ms: int = DEFAULT_INITIAL_DELAY_MS
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    max_delay_ms: int = 30_000

    def delay_seconds(self, attempt: int) -> float:
        """Return the sleep before the *attempt*-th retry (1-indexed)."""
        if attempt <= 0:
            return 0.0
        ms = self.initial_delay_ms * (self.backoff_factor ** (attempt - 1))
        return min(ms, self.max_delay_ms) / 1000.0


class DedupCache:
    """In-memory LRU + on-disk window keyed by ``event_id``.

    The on-disk window survives orchestrator restarts so a crash loop
    cannot spam a sink. The cache's first lookup also seeds the LRU
    from disk so repeated misses don't pay the IO cost.

    Args:
        path: JSONL file storing ``{"event_id": "...", "ts": <unix>}``.
        lru_size: In-memory LRU capacity.
        window_seconds: Entries older than this are pruned on write.
    """

    def __init__(
        self,
        path: Path,
        *,
        lru_size: int = DEFAULT_DEDUP_LRU_SIZE,
        window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
    ) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lru: OrderedDict[str, float] = OrderedDict()
        self._lru_size = max(1, lru_size)
        self._window_seconds = max(1, window_seconds)
        self._loaded = False

    def seen(self, event_id: str, *, now: float | None = None) -> bool:
        """Return ``True`` if ``event_id`` is within the dedup window."""
        if not event_id:
            return False
        ts = now if now is not None else time.time()
        self._ensure_loaded()
        cached = self._lru.get(event_id)
        if cached is not None and (ts - cached) < self._window_seconds:
            self._lru.move_to_end(event_id)
            return True
        return False

    def remember(self, event_id: str, *, now: float | None = None) -> None:
        """Mark ``event_id`` as seen; persists to disk."""
        if not event_id:
            return
        ts = now if now is not None else time.time()
        self._ensure_loaded()
        self._lru[event_id] = ts
        self._lru.move_to_end(event_id)
        while len(self._lru) > self._lru_size:
            self._lru.popitem(last=False)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event_id": event_id, "ts": ts}) + "\n")
        except OSError as exc:
            logger.warning("Could not persist dedup entry to %s: %s", self._path, exc)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        cutoff = time.time() - self._window_seconds
        try:
            for raw in self._path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = float(rec.get("ts", 0.0))
                if ts < cutoff:
                    continue
                eid = str(rec.get("event_id", ""))
                if eid:
                    self._lru[eid] = ts
            while len(self._lru) > self._lru_size:
                self._lru.popitem(last=False)
        except OSError as exc:
            logger.warning("Could not seed dedup cache from %s: %s", self._path, exc)


class DeadLetter:
    """Append-only JSONL sink for permanently-failed deliveries.

    The file is rotated when it crosses ``max_bytes`` (renamed to
    ``dead_letter.jsonl.<timestamp>`` so the active file stays small).

    Args:
        path: Path to ``dead_letter.jsonl``. Parents are created.
        max_bytes: Rotation threshold.
    """

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_DEAD_LETTER_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so consumers (and the verification block in
        # the ticket) can `test -f` it immediately.
        if not self._path.exists():
            self._path.touch()

    @property
    def path(self) -> Path:
        """Return the active dead-letter file path."""
        return self._path

    def append(self, sink_id: str, event: NotificationEvent, reason: str) -> None:
        """Append a permanent-failure record."""
        record = {
            "ts": time.time(),
            "sink_id": sink_id,
            "reason": reason,
            "event": event.to_payload(),
        }
        self._maybe_rotate()
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            logger.error("Failed to append dead-letter record for %s: %s", sink_id, exc)

    def _maybe_rotate(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        rotated = self._path.with_suffix(self._path.suffix + f".{int(time.time())}")
        try:
            self._path.rename(rotated)
            self._path.touch()
        except OSError as exc:
            logger.warning("Could not rotate dead-letter file %s: %s", self._path, exc)


class NotificationDispatcher:
    """Routes events to sinks with retry, dedup, and dead-lettering.

    The dispatcher is the public façade the lifecycle bridge calls.
    A dispatcher owns a single shared :class:`DedupCache` and
    :class:`DeadLetter`; per-sink configuration lives on the sink
    itself.

    Args:
        runtime_dir: Base directory under which ``notifications/`` is
            created (typically ``.sdd/runtime``).
        retry: Default retry policy applied when a sink does not
            override it via ``getattr(sink, 'retry_policy', None)``.
        audit_hook: Optional callable invoked once per terminal
            outcome with ``(actor, resource_type, resource_id,
            details)`` so the caller can append to the HMAC chain
            without the dispatcher importing the audit module.
        clock: Injection seam for tests.
        sleeper: Injection seam for tests; defaults to
            :func:`asyncio.sleep`.
    """

    def __init__(
        self,
        runtime_dir: Path,
        *,
        retry: RetryPolicy | None = None,
        audit_hook: Callable[[str, str, str, dict[str, object]], None] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        notif_dir = runtime_dir / "notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)
        self._dedup = DedupCache(notif_dir / "dedup.jsonl")
        self._dead_letter = DeadLetter(notif_dir / "dead_letter.jsonl")
        self._retry = retry or RetryPolicy()
        self._audit_hook = audit_hook
        self._clock = clock or time.time
        self._sleeper: Callable[[float], Awaitable[None]] = sleeper or asyncio.sleep

    @property
    def dead_letter(self) -> DeadLetter:
        """Expose the dead-letter handle so callers can inspect / rotate."""
        return self._dead_letter

    @property
    def dedup(self) -> DedupCache:
        """Expose the dedup cache (mostly for tests / metrics)."""
        return self._dedup

    async def dispatch(
        self,
        event: NotificationEvent,
        sinks: Iterable[NotificationSink],
    ) -> dict[str, NotificationOutcome]:
        """Fan ``event`` out to every sink, returning per-sink outcomes."""
        outcomes: dict[str, NotificationOutcome] = {}
        targets = list(sinks)
        if self._dedup.seen(event.event_id, now=self._clock()):
            for sink in targets:
                outcomes[sink.sink_id] = NotificationOutcome.DEDUPLICATED
                self._record_audit(sink.sink_id, event, NotificationOutcome.DEDUPLICATED, attempts=0)
            return outcomes

        # Mark as seen up-front so a concurrent restart is also skipped.
        self._dedup.remember(event.event_id, now=self._clock())

        for sink in targets:
            outcomes[sink.sink_id] = await self._deliver_one(sink, event)
        return outcomes

    async def _deliver_one(
        self,
        sink: NotificationSink,
        event: NotificationEvent,
    ) -> NotificationOutcome:
        retry: RetryPolicy = getattr(sink, "retry_policy", None) or self._retry
        last_error: BaseException | None = None
        for attempt in range(1, retry.max_attempts + 1):
            try:
                await sink.deliver(event)
            except NotificationPermanentError as exc:
                self._dead_letter.append(sink.sink_id, event, f"permanent:{exc}")
                self._record_audit(
                    sink.sink_id,
                    event,
                    NotificationOutcome.FAILED_PERMANENT,
                    attempts=attempt,
                    error=str(exc),
                )
                return NotificationOutcome.FAILED_PERMANENT
            except NotificationDeliveryError as exc:
                last_error = exc
                if attempt >= retry.max_attempts:
                    break
                self._record_audit(
                    sink.sink_id,
                    event,
                    NotificationOutcome.FAILED_RETRYING,
                    attempts=attempt,
                    error=str(exc),
                )
                await self._sleeper(retry.delay_seconds(attempt))
            except Exception as exc:
                last_error = exc
                if attempt >= retry.max_attempts:
                    break
                self._record_audit(
                    sink.sink_id,
                    event,
                    NotificationOutcome.FAILED_RETRYING,
                    attempts=attempt,
                    error=str(exc),
                )
                await self._sleeper(retry.delay_seconds(attempt))
            else:
                self._record_audit(
                    sink.sink_id,
                    event,
                    NotificationOutcome.DELIVERED,
                    attempts=attempt,
                )
                return NotificationOutcome.DELIVERED

        # Exhausted all retries.
        reason = f"retries_exhausted:{last_error}"
        self._dead_letter.append(sink.sink_id, event, reason)
        self._record_audit(
            sink.sink_id,
            event,
            NotificationOutcome.FAILED_PERMANENT,
            attempts=retry.max_attempts,
            error=str(last_error) if last_error else "unknown",
        )
        return NotificationOutcome.FAILED_PERMANENT

    def _record_audit(
        self,
        sink_id: str,
        event: NotificationEvent,
        outcome: NotificationOutcome,
        *,
        attempts: int,
        error: str | None = None,
    ) -> None:
        if self._audit_hook is None:
            return
        details: dict[str, object] = {
            "event_id": event.event_id,
            "sink_id": sink_id,
            "outcome": outcome.value,
            "kind": event.kind.value,
            "attempts": attempts,
        }
        if error is not None:
            details["error"] = error
        try:
            self._audit_hook(
                f"sink:{sink_id}",
                "notification",
                event.event_id or "<missing>",
                details,
            )
        except Exception as exc:
            logger.warning("audit hook raised for sink=%s outcome=%s: %s", sink_id, outcome.value, exc)


# Suppress unused import warning for Any when only used in TYPE_CHECKING.
_ = Any
