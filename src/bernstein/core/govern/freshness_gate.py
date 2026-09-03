"""Freshness-gated artifact reads with producer triggering and stampede prevention (#5130).

Gates reads of governance artifacts (such as cluster inventory, audit reports,
finding collections) on their age. If an artifact is older than its TTL, or
missing, the gate triggers the producer to refresh it and blocks concurrent readers
until a terminal state is reached.

When `no_wait=True` is requested, the gate serves the stale artifact immediately
with `is_stale=True` and populates `stale_reason` in the response body, eliminating
thundering-herd stampedes on expensive collectors.

Terminal State Definition:
    Both `SUCCESS` and `FAILED` count as terminal states. When a producer run
    finishes (whether by returning normally or by raising an exception), it
    transitions to a terminal state (`ProducerState.SUCCESS` or `ProducerState.FAILED`)
    and unblocks all waiting readers. If a stale artifact is available, waiting
    readers receive it with `is_stale=True` and `stale_reason` containing the error
    detail. If no prior artifact is available, the exception is propagated to the
    waiting readers. This prevents deadlocks where readers wait forever on a failed
    producer.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "FreshnessGate",
    "FreshnessResult",
    "ProducerState",
    "freshness_gated_read",
]


class ProducerState(StrEnum):
    """Lifecycle states of an artifact producer."""

    IDLE = "idle"
    RUNNING = "running"
    PROGRESS = "progress"
    SUCCESS = "success"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Return True if this state represents a completed run."""
        return self in (ProducerState.SUCCESS, ProducerState.FAILED)


@dataclass(frozen=True)
class FreshnessResult[T]:
    """Result of a freshness-gated read operation.

    Attributes:
        data: The retrieved artifact data, or None if unavailable.
        is_stale: True if the served artifact is older than the configured TTL
            or if the producer failed and stale data was served as fallback.
        stale_reason: Human-readable reason why the artifact is considered stale.
        generated_at: Timestamp when the artifact was produced.
        age_seconds: Wall-clock age of the artifact in seconds at read time.
        producer_state: Terminal or current state of the associated producer.
    """

    data: T | None
    is_stale: bool = False
    stale_reason: str | None = None
    generated_at: datetime | None = None
    age_seconds: float = 0.0
    producer_state: ProducerState = ProducerState.IDLE

    def unwrap(self) -> T:
        """Return the underlying data, raising ValueError if None."""
        if self.data is None:
            raise ValueError(f"No artifact data available (stale_reason={self.stale_reason!r})")
        return self.data


@dataclass
class _ProducerContext:
    """Internal coordination context for a single artifact key."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    event: threading.Event = field(default_factory=threading.Event)
    state: ProducerState = ProducerState.IDLE
    error: BaseException | None = None
    last_produced_data: Any | None = None
    last_produced_at: datetime | None = None
    progress_count: int = 0


class FreshnessGate[T]:
    """Coordinates freshness checks, producer runs, and reader synchronization."""

    def __init__(self) -> None:
        self._global_lock = threading.Lock()
        self._contexts: dict[str, _ProducerContext] = {}

    def _get_context(self, key: str) -> _ProducerContext:
        with self._global_lock:
            if key not in self._contexts:
                self._contexts[key] = _ProducerContext()
            return self._contexts[key]

    def read(
        self,
        key: str,
        *,
        reader: Callable[[], tuple[T | None, datetime | float | None] | T | None],
        producer: Callable[..., Any],
        ttl_seconds: float,
        no_wait: bool = False,
        now: datetime | None = None,
        timeout_seconds: float | None = None,
        progress_callback: Callable[[Any], None] | None = None,
    ) -> FreshnessResult[T]:
        """Read an artifact, triggering producer if stale or missing.

        Args:
            key: Logical key or name identifying the artifact stream.
            reader: Callable returning either `(data, timestamp)` or `data`.
            producer: Callable invoked to produce or refresh the artifact.
            ttl_seconds: Freshness window in seconds.
            no_wait: If True, returns stale data immediately with `is_stale=True`
                without blocking on the producer.
            now: Injected clock for deterministic testing (defaults to UTC now).
            timeout_seconds: Maximum time in seconds to wait for a producer run.
            progress_callback: Optional callback for intermediate progress.

        Returns:
            A populated :class:`FreshnessResult[T]`.
        """
        current_time = now if now is not None else datetime.now(UTC)

        # 1. Probe current artifact via reader
        data, generated_at, age_seconds = self._read_and_derive_age(reader, current_time)

        # 2. Check if fresh
        is_fresh = data is not None and age_seconds <= ttl_seconds
        if is_fresh:
            return FreshnessResult(
                data=data,
                is_stale=False,
                stale_reason=None,
                generated_at=generated_at,
                age_seconds=age_seconds,
                producer_state=ProducerState.SUCCESS,
            )

        # 3. Artifact is stale or missing
        stale_reason = (
            f"Artifact age {age_seconds:.1f}s exceeds TTL {ttl_seconds:.1f}s"
            if data is not None
            else "Artifact does not exist"
        )

        ctx = self._get_context(key)

        # 4. Handle no_wait escape hatch
        if no_wait and data is not None:
            # Trigger background refresh if not already running
            self._trigger_async_if_idle(ctx, producer, progress_callback)
            return FreshnessResult(
                data=data,
                is_stale=True,
                stale_reason=stale_reason,
                generated_at=generated_at,
                age_seconds=age_seconds,
                producer_state=ctx.state,
            )

        # 5. Blocking read with single-producer coordination (stampede guard)
        return self._execute_or_wait(
            ctx=ctx,
            key=key,
            reader=reader,
            producer=producer,
            stale_data=data,
            stale_generated_at=generated_at,
            stale_age_seconds=age_seconds,
            stale_reason=stale_reason,
            ttl_seconds=ttl_seconds,
            current_time=current_time,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
        )

    def _read_and_derive_age(
        self,
        reader: Callable[[], tuple[T | None, datetime | float | None] | T | None],
        current_time: datetime,
    ) -> tuple[T | None, datetime | None, float]:
        try:
            read_val = reader()
        except Exception as exc:
            logger.warning("freshness_gate: reader failed: %s", exc)
            return None, None, float("inf")

        if isinstance(read_val, tuple) and len(read_val) == 2:
            data, ts_val = read_val
        else:
            data = read_val  # type: ignore[assignment]
            ts_val = getattr(data, "generated_at", None) or getattr(data, "timestamp", None)

        if data is None:
            return None, None, float("inf")

        if ts_val is None:
            return data, None, float("inf")

        if isinstance(ts_val, (int, float)):
            gen_dt = datetime.fromtimestamp(ts_val, tz=UTC)
        elif isinstance(ts_val, datetime):
            gen_dt = ts_val if ts_val.tzinfo is not None else ts_val.replace(tzinfo=UTC)
        else:
            return data, None, float("inf")

        age_seconds = max(0.0, (current_time - gen_dt).total_seconds())
        return data, gen_dt, age_seconds

    def _trigger_async_if_idle(
        self,
        ctx: _ProducerContext,
        producer: Callable[..., Any],
        progress_callback: Callable[[Any], None] | None,
    ) -> None:
        with ctx.lock:
            if ctx.state == ProducerState.RUNNING:
                return
            ctx.state = ProducerState.RUNNING
            ctx.event.clear()
            ctx.error = None

        thread = threading.Thread(
            target=self._run_producer_sync,
            args=(ctx, producer, progress_callback),
            daemon=True,
        )
        thread.start()

    def _execute_or_wait(
        self,
        ctx: _ProducerContext,
        key: str,
        reader: Callable[[], tuple[T | None, datetime | float | None] | T | None],
        producer: Callable[..., Any],
        stale_data: T | None,
        stale_generated_at: datetime | None,
        stale_age_seconds: float,
        stale_reason: str,
        ttl_seconds: float,
        current_time: datetime,
        timeout_seconds: float | None,
        progress_callback: Callable[[Any], None] | None,
    ) -> FreshnessResult[T]:
        should_run = False
        with ctx.lock:
            if ctx.state != ProducerState.RUNNING:
                ctx.state = ProducerState.RUNNING
                ctx.event.clear()
                ctx.error = None
                should_run = True

        if should_run:
            # We are the designated producer runner
            self._run_producer_sync(ctx, producer, progress_callback)
        else:
            # Wait for the active producer to reach a terminal state
            finished = ctx.event.wait(timeout=timeout_seconds)
            if not finished:
                logger.warning("freshness_gate: timed out waiting for producer for %r", key)
                if stale_data is not None:
                    return FreshnessResult(
                        data=stale_data,
                        is_stale=True,
                        stale_reason=f"Timed out waiting for producer: {stale_reason}",
                        generated_at=stale_generated_at,
                        age_seconds=stale_age_seconds,
                        producer_state=ctx.state,
                    )
                raise TimeoutError(f"Timed out waiting for producer for {key!r}")

        # Post-terminal evaluation
        if ctx.state == ProducerState.FAILED:
            if stale_data is not None:
                return FreshnessResult(
                    data=stale_data,
                    is_stale=True,
                    stale_reason=f"Producer failed ({ctx.error}): {stale_reason}",
                    generated_at=stale_generated_at,
                    age_seconds=stale_age_seconds,
                    producer_state=ProducerState.FAILED,
                )
            if ctx.error is not None:
                raise ctx.error
            raise RuntimeError(f"Producer failed for {key!r}")

        # Producer succeeded - re-read freshly written artifact
        new_data, new_gen_at, new_age = self._read_and_derive_age(reader, current_time)
        if new_data is not None:
            return FreshnessResult(
                data=new_data,
                is_stale=new_age > ttl_seconds,
                stale_reason=(
                    f"Produced artifact age {new_age:.1f}s exceeds TTL {ttl_seconds:.1f}s"
                    if new_age > ttl_seconds
                    else None
                ),
                generated_at=new_gen_at,
                age_seconds=new_age,
                producer_state=ProducerState.SUCCESS,
            )

        # Fallback to in-memory returned payload if reader did not persist to disk
        if ctx.last_produced_data is not None:
            return FreshnessResult(
                data=ctx.last_produced_data,
                is_stale=False,
                stale_reason=None,
                generated_at=ctx.last_produced_at or current_time,
                age_seconds=0.0,
                producer_state=ProducerState.SUCCESS,
            )

        if stale_data is not None:
            return FreshnessResult(
                data=stale_data,
                is_stale=True,
                stale_reason="Producer finished but no new data was readable",
                generated_at=stale_generated_at,
                age_seconds=stale_age_seconds,
                producer_state=ProducerState.SUCCESS,
            )

        raise RuntimeError(f"Producer finished for {key!r} but no artifact data was returned or read")

    def _run_producer_sync(
        self,
        ctx: _ProducerContext,
        producer: Callable[..., Any],
        progress_callback: Callable[[Any], None] | None,
    ) -> None:
        def _report_progress(update: Any = None) -> None:
            with ctx.lock:
                ctx.progress_count += 1
                ctx.state = ProducerState.PROGRESS
            if progress_callback is not None:
                try:
                    progress_callback(update)
                except Exception as exc:
                    logger.debug("freshness_gate: progress_callback error: %s", exc)

        try:
            # Check if producer accepts progress callback
            import inspect

            sig = inspect.signature(producer) if callable(producer) else None
            if sig and ("progress_callback" in sig.parameters or "report_progress" in sig.parameters):
                kw = "progress_callback" if "progress_callback" in sig.parameters else "report_progress"
                result = producer(**{kw: _report_progress})
            else:
                result = producer()

            with ctx.lock:
                ctx.last_produced_data = result
                ctx.last_produced_at = datetime.now(UTC)
                ctx.state = ProducerState.SUCCESS
                ctx.error = None
        except BaseException as exc:
            logger.warning("freshness_gate: producer raised exception: %s", exc)
            with ctx.lock:
                ctx.state = ProducerState.FAILED
                ctx.error = exc
        finally:
            with ctx.lock:
                ctx.event.set()


_DEFAULT_GATE: FreshnessGate[Any] = FreshnessGate()


def freshness_gated_read[T](
    key: str,
    *,
    reader: Callable[[], tuple[T | None, datetime | float | None] | T | None],
    producer: Callable[..., Any],
    ttl_seconds: float,
    no_wait: bool = False,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
    progress_callback: Callable[[Any], None] | None = None,
    gate: FreshnessGate[T] | None = None,
) -> FreshnessResult[T]:
    """Convenience function for freshness-gated reads using a shared or custom gate."""
    effective_gate = gate if gate is not None else _DEFAULT_GATE
    return effective_gate.read(
        key=key,
        reader=reader,
        producer=producer,
        ttl_seconds=ttl_seconds,
        no_wait=no_wait,
        now=now,
        timeout_seconds=timeout_seconds,
        progress_callback=progress_callback,
    )
