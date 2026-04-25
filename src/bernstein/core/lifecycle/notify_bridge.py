"""Glue between :mod:`bernstein.core.lifecycle.hooks` and the notification subsystem.

A single :class:`NotifyLifecycleBridge` subscribes to the five lifecycle
hook points exposed in v1.8.15 (``pre_task``, ``post_task``,
``pre_merge``, ``post_merge``, ``pre_spawn``) and dispatches a
:class:`~bernstein.core.notifications.protocol.NotificationEvent` to
every enabled sink whose ``events`` allow-list permits the kind.

The bridge owns:

  * a :class:`NotificationDispatcher` (retry/dedup/dead-letter/audit),
  * the loaded :class:`NotificationsConfig`, and
  * a list of live sinks built from that config (or supplied
    explicitly for tests).

Wiring is a one-shot call to
:meth:`NotifyLifecycleBridge.attach_to_registry` from the bootstrap
sequence; nothing else in the codebase imports the notification
subsystem to spell out hook handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
import time
from typing import TYPE_CHECKING

from bernstein.core.lifecycle.hooks import HookRegistry, LifecycleContext, LifecycleEvent
from bernstein.core.notifications.bridge import (
    NotificationDispatcher,
    RetryPolicy,
)
from bernstein.core.notifications.config import NotificationsConfig, SinkSchema
from bernstein.core.notifications.protocol import (
    NotificationEvent,
    NotificationEventKind,
)
from bernstein.core.notifications.registry import build_sink, register_sink

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from concurrent.futures import Future
    from pathlib import Path

    from bernstein.core.notifications.protocol import NotificationSink

logger = logging.getLogger(__name__)

__all__ = [
    "NotifyLifecycleBridge",
    "build_bridge_from_config",
    "lifecycle_event_to_kind",
]


_EVENT_KIND_MAP: dict[LifecycleEvent, NotificationEventKind] = {
    LifecycleEvent.PRE_TASK: NotificationEventKind.PRE_TASK,
    LifecycleEvent.POST_TASK: NotificationEventKind.POST_TASK,
    LifecycleEvent.PRE_MERGE: NotificationEventKind.PRE_MERGE,
    LifecycleEvent.POST_MERGE: NotificationEventKind.POST_MERGE,
    LifecycleEvent.PRE_SPAWN: NotificationEventKind.PRE_SPAWN,
    LifecycleEvent.POST_SPAWN: NotificationEventKind.POST_SPAWN,
}


def lifecycle_event_to_kind(event: LifecycleEvent) -> NotificationEventKind:
    """Convert a lifecycle event to the notification kind enum."""
    return _EVENT_KIND_MAP[event]


class NotifyLifecycleBridge:
    """Subscribes to lifecycle hooks and fans events out to sinks."""

    #: Lifecycle events the bridge wires by default. ``post_spawn`` is
    #: deliberately excluded from this list per the ticket scope (only
    #: ``pre_spawn`` is in scope), but the bridge still routes
    #: ``post_spawn`` if a hook is fired manually.
    DEFAULT_EVENTS: tuple[LifecycleEvent, ...] = (
        LifecycleEvent.PRE_TASK,
        LifecycleEvent.POST_TASK,
        LifecycleEvent.PRE_MERGE,
        LifecycleEvent.POST_MERGE,
        LifecycleEvent.PRE_SPAWN,
    )

    def __init__(
        self,
        dispatcher: NotificationDispatcher,
        sinks: Iterable[NotificationSink],
        sink_configs: dict[str, SinkSchema],
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._sinks: list[NotificationSink] = list(sinks)
        self._sink_configs = sink_configs
        self._clock = clock or time.time
        self._lock = threading.Lock()
        # Reusable event loop for the synchronous hook callable. We
        # spin one up lazily so tests using their own loop aren't
        # affected.
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def sinks(self) -> list[NotificationSink]:
        """Return a snapshot of the live sinks the bridge owns."""
        return list(self._sinks)

    def attach_to_registry(
        self,
        registry: HookRegistry,
        *,
        events: Iterable[LifecycleEvent] | None = None,
    ) -> None:
        """Subscribe the bridge to ``events`` on ``registry``.

        Args:
            registry: The lifecycle :class:`HookRegistry` shared by the
                orchestrator.
            events: Subset of events to subscribe to. ``None`` uses
                :attr:`DEFAULT_EVENTS`.
        """
        for event in events or self.DEFAULT_EVENTS:
            registry.register_callable(event, self._make_hook(event))

    async def dispatch_event(self, event: NotificationEvent) -> None:
        """Async-friendly fan-out used by tests and the CLI test command."""
        targets = self._select_sinks(event)
        if not targets:
            return
        await self._dispatcher.dispatch(event, targets)

    async def aclose(self) -> None:
        """Close every sink we own and release the dedicated loop."""
        for sink in self._sinks:
            try:
                await sink.close()
            except Exception as exc:
                logger.warning("error closing sink %s: %s", sink.sink_id, exc)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
            self._loop = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_hook(self, lifecycle_event: LifecycleEvent) -> Callable[[LifecycleContext], None]:
        def _hook(ctx: LifecycleContext) -> None:
            event = self._build_event(lifecycle_event, ctx)
            future = self._submit_async(event)
            # We deliberately don't await the future from within the
            # synchronous hook — completion happens in the background
            # loop so a slow webhook doesn't stall the orchestrator.
            del future

        _hook.__qualname__ = f"NotifyLifecycleBridge.hook[{lifecycle_event.value}]"
        return _hook

    def _build_event(self, lifecycle_event: LifecycleEvent, ctx: LifecycleContext) -> NotificationEvent:
        kind = lifecycle_event_to_kind(lifecycle_event)
        title = _humanise_title(lifecycle_event, ctx)
        body = _humanise_body(ctx)
        event_id = _stable_event_id(lifecycle_event, ctx)
        severity = _infer_severity(lifecycle_event, ctx)
        return NotificationEvent(
            event_id=event_id,
            kind=kind,
            title=title,
            body=body,
            severity=severity,
            task_id=ctx.task,
            session_id=ctx.session_id,
            timestamp=self._clock(),
        )

    def _select_sinks(self, event: NotificationEvent) -> list[NotificationSink]:
        targets: list[NotificationSink] = []
        for sink in self._sinks:
            cfg = self._sink_configs.get(sink.sink_id)
            if cfg is None or not cfg.enabled:
                continue
            if cfg.events is not None and event.kind.value not in cfg.events:
                continue
            if cfg.severities is not None and event.severity not in cfg.severities:
                continue
            targets.append(sink)
        return targets

    def _submit_async(self, event: NotificationEvent) -> Future[None]:
        loop = self._ensure_loop()
        coro = self.dispatch_event(event)
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop
            loop = asyncio.new_event_loop()

            def _runner() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(
                target=_runner,
                name="bernstein-notify-loop",
                daemon=True,
            )
            thread.start()
            self._loop = loop
            return loop


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_bridge_from_config(
    config: NotificationsConfig,
    *,
    runtime_dir: Path,
    audit_hook: Callable[[str, str, str, dict[str, object]], None] | None = None,
    extra_sinks: Iterable[NotificationSink] | None = None,
    register_in_registry: bool = True,
) -> NotifyLifecycleBridge:
    """Construct a :class:`NotifyLifecycleBridge` from a parsed config.

    Args:
        config: Parsed :class:`NotificationsConfig`.
        runtime_dir: Where dedup/dead-letter files live (``.sdd/runtime``).
        audit_hook: Optional function the dispatcher calls per terminal
            outcome so callers can append to the HMAC chain.
        extra_sinks: Pre-built sinks to merge in (used by tests and by
            callers that already own a configured driver instance, like
            the in-process Telegram chat bridge).
        register_in_registry: When ``True``, also register every built
            sink in :func:`bernstein.core.notifications.registry.default_registry`
            so other callers can ``get_sink(...)``.
    """
    retry = RetryPolicy(
        max_attempts=config.retry.max_attempts,
        initial_delay_ms=config.retry.initial_delay_ms,
        backoff_factor=config.retry.backoff_factor,
        max_delay_ms=config.retry.max_delay_ms,
    )
    dispatcher = NotificationDispatcher(
        runtime_dir,
        retry=retry,
        audit_hook=audit_hook,
    )

    sinks: list[NotificationSink] = []
    sink_configs: dict[str, SinkSchema] = {}
    for sink_cfg in config.sinks:
        sink_configs[sink_cfg.id] = sink_cfg
        if not sink_cfg.enabled:
            continue
        try:
            sink = build_sink(sink_cfg.model_dump())
        except Exception as exc:
            logger.error(
                "failed to build notification sink %r (kind=%s): %s",
                sink_cfg.id,
                sink_cfg.kind,
                exc,
            )
            continue
        sinks.append(sink)
        if register_in_registry:
            # Duplicate registration in the same process — ignore; the
            # bridge still owns its own list.
            with contextlib.suppress(ValueError):
                register_sink(sink)

    if extra_sinks:
        for sink in extra_sinks:
            sinks.append(sink)
            if register_in_registry:
                with contextlib.suppress(ValueError):
                    register_sink(sink)
            sink_configs.setdefault(
                sink.sink_id,
                SinkSchema(id=sink.sink_id, kind=getattr(sink, "kind", "unknown")),
            )

    return NotifyLifecycleBridge(dispatcher, sinks, sink_configs)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _humanise_title(event: LifecycleEvent, ctx: LifecycleContext) -> str:
    base = {
        LifecycleEvent.PRE_TASK: "Task starting",
        LifecycleEvent.POST_TASK: "Task finished",
        LifecycleEvent.PRE_MERGE: "Merge starting",
        LifecycleEvent.POST_MERGE: "Merge finished",
        LifecycleEvent.PRE_SPAWN: "Agent spawning",
        LifecycleEvent.POST_SPAWN: "Agent spawned",
    }[event]
    if ctx.task:
        return f"{base}: {ctx.task}"
    if ctx.session_id:
        return f"{base}: {ctx.session_id}"
    return base


def _humanise_body(ctx: LifecycleContext) -> str:
    lines: list[str] = []
    if ctx.task:
        lines.append(f"task: {ctx.task}")
    if ctx.session_id:
        lines.append(f"session: {ctx.session_id}")
    if ctx.workdir:
        lines.append(f"workdir: {ctx.workdir}")
    return "\n".join(lines)


def _infer_severity(event: LifecycleEvent, ctx: LifecycleContext) -> str:
    # The hook contract doesn't carry an explicit pass/fail field;
    # callers can stuff it on ctx.env if they care. Bernstein's
    # bootstrap will eventually surface a "post_task_failed"
    # discriminator in env; in the meantime treat unknown as info.
    outcome = ctx.env.get("BERNSTEIN_TASK_OUTCOME", "").lower()
    if outcome in {"failed", "error"}:
        return "error"
    if outcome in {"warning", "warn"}:
        return "warning"
    if event in {LifecycleEvent.POST_TASK, LifecycleEvent.POST_MERGE} and outcome == "rolled_back":
        return "warning"
    return "info"


def _stable_event_id(event: LifecycleEvent, ctx: LifecycleContext) -> str:
    """Derive a deterministic event id so retries dedup correctly.

    The id mixes the event kind with whatever scope identifiers are
    available so multiple bursts for the same task don't collapse.
    Callers that want explicit control can pre-compute an id and pass
    it through ``ctx.env['BERNSTEIN_NOTIFY_EVENT_ID']``.
    """
    explicit = ctx.env.get("BERNSTEIN_NOTIFY_EVENT_ID")
    if explicit:
        return explicit
    parts = [
        event.value,
        ctx.task or "",
        ctx.session_id or "",
        f"{ctx.timestamp:.3f}",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]
