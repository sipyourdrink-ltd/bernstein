"""Write-through buffered sink for cloud-durability without WAL latency.

The WAL's crash-safety contract requires a ``durable=True`` write to
be on stable storage before ``write`` returns. Synchronous PUT to S3
on every append would tank throughput to the network round-trip time,
while pure-async mirrors would break the crash invariant.

:class:`BufferedSink` splits the difference: the caller's write is
committed locally with the full fsync semantics of
:class:`~bernstein.core.storage.sinks.local_fs.LocalFsSink`, then the
same payload is queued for a best-effort asynchronous mirror to a
remote sink. The WAL invariant is preserved (local fsync survives a
process crash) and the cloud durability guarantee is layered on top
(the mirror survives host loss as soon as the queue drains).

The queue is bounded: when the remote sink lags far enough that the
queue saturates, producers block until the mirror catches up. This
gives deterministic back-pressure rather than silently dropping
mirrors or unbounded memory growth.

``close()`` blocks until every pending mirror has either ACKed or
failed, so shutdown cleanly flushes the tail — the orchestrator uses
this when winding down a run so no uploads are lost on a clean exit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from bernstein.core.storage.sink import (
    ArtifactSink,
    ArtifactStat,
    SinkError,
)

logger = logging.getLogger(__name__)


@dataclass
class _PendingMirror:
    """One queued mirror job.

    Attributes:
        key: Logical sink key.
        data: Payload to upload.
        content_type: Optional MIME type hint for the remote sink.
        enqueued_at: Monotonic seconds at enqueue (used for lag metrics).
    """

    key: str
    data: bytes
    content_type: str | None
    enqueued_at: float = field(default_factory=time.monotonic)


@dataclass
class BufferedSinkStats:
    """Snapshot of :class:`BufferedSink` observability counters.

    The fields map 1:1 to the Prometheus metrics described in the
    ticket and are also useful for unit-test assertions without having
    to stand up the metrics registry.

    Attributes:
        pending_writes: Size of the queue at observation time.
        completed_mirrors: Total successful remote mirrors since start.
        failed_mirrors: Total failed remote mirrors since start.
        oldest_pending_age_seconds: Age of the oldest queued item
            (``0.0`` when the queue is empty).
    """

    pending_writes: int
    completed_mirrors: int
    failed_mirrors: int
    oldest_pending_age_seconds: float


class BufferedSink(ArtifactSink):
    """Local-fsync-then-mirror sink.

    Writes go to *local* synchronously (preserving the WAL fsync
    invariant); a background task then streams the same payload to
    *remote*. Reads check the remote sink first — on a fresh startup
    with an ephemeral local disk this is how the crash-recovery path
    finds the last committed state — and fall back to local when the
    remote cannot serve the key.

    The buffer is bounded (``max_pending``): producer writes block
    when the queue is full so a slow remote sink applies back-pressure
    instead of being silently dropped.
    """

    name: str = "buffered"

    def __init__(
        self,
        *,
        local: ArtifactSink,
        remote: ArtifactSink,
        max_pending: int = 1024,
    ) -> None:
        """Create the buffered sink.

        Args:
            local: The synchronous, crash-safe sink (normally a
                :class:`~bernstein.core.storage.sinks.local_fs.LocalFsSink`
                pointing at ``.sdd/``).
            remote: The asynchronous, durable sink to mirror to.
            max_pending: Hard cap on the mirror queue depth. Smaller
                values give tighter memory bounds at the cost of more
                producer blocking when the mirror lags.
        """
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self._local = local
        self._remote = remote
        self._queue: asyncio.Queue[_PendingMirror | None] = asyncio.Queue(
            maxsize=max_pending,
        )
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._completed = 0
        self._failed = 0
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain_loop(),
                name="bernstein-buffered-sink-drain",
            )

    async def _drain_loop(self) -> None:
        """Consume queued mirrors one at a time until shutdown."""
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is None:
                        # Sentinel: drain requested.
                        return
                    try:
                        await self._remote.write(
                            item.key,
                            item.data,
                            durable=True,
                            content_type=item.content_type,
                        )
                        self._completed += 1
                    except Exception as exc:
                        self._failed += 1
                        logger.warning(
                            "BufferedSink mirror of %r failed: %s",
                            item.key,
                            exc,
                        )
                finally:
                    self._queue.task_done()
        finally:
            self._stopped.set()

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def stats(self) -> BufferedSinkStats:
        """Return a snapshot of the sink's observability counters."""
        pending = self._queue.qsize()
        oldest_age = 0.0
        # Peek at the head without consuming it. ``asyncio.Queue._queue``
        # is a ``collections.deque`` — accessing it is safe for an
        # observational peek but pyright can't see through the private
        # attribute, so the cast to ``list[object]`` keeps strict typing
        # happy.
        raw_items: list[object] = list(self._queue._queue)  # type: ignore[attr-defined]
        if raw_items:
            oldest = raw_items[0]
            if isinstance(oldest, _PendingMirror):
                oldest_age = max(0.0, time.monotonic() - oldest.enqueued_at)
        return BufferedSinkStats(
            pending_writes=pending,
            completed_mirrors=self._completed,
            failed_mirrors=self._failed,
            oldest_pending_age_seconds=oldest_age,
        )

    # ------------------------------------------------------------------
    # ArtifactSink protocol
    # ------------------------------------------------------------------

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        if self._closed:
            raise SinkError("BufferedSink already closed")

        # 1. Synchronously commit locally — preserves WAL fsync invariant.
        await self._local.write(
            key,
            data,
            durable=durable,
            content_type=content_type,
        )

        # 2. Queue the same payload for the remote mirror.
        self._ensure_worker()
        pending = _PendingMirror(
            key=key,
            data=data,
            content_type=content_type,
        )
        await self._queue.put(pending)

    async def read(self, key: str) -> bytes:
        # Prefer remote — this is the crash-recovery path where the
        # local cache may have been on ephemeral storage.
        try:
            return await self._remote.read(key)
        except FileNotFoundError:
            return await self._local.read(key)
        except SinkError as exc:
            logger.debug("BufferedSink remote read for %r failed: %s", key, exc)
            return await self._local.read(key)

    async def list(self, prefix: str) -> list[str]:
        # Merge both views: local may have entries not yet mirrored,
        # remote may have entries the local cache never touched (fresh
        # recovery).
        local_keys = await self._local.list(prefix)
        try:
            remote_keys = await self._remote.list(prefix)
        except SinkError as exc:
            logger.debug("BufferedSink remote list for %r failed: %s", prefix, exc)
            remote_keys = []
        merged = sorted(set(local_keys) | set(remote_keys))
        return merged

    async def delete(self, key: str) -> None:
        await self._local.delete(key)
        try:
            await self._remote.delete(key)
        except SinkError as exc:
            logger.warning("BufferedSink remote delete for %r failed: %s", key, exc)

    async def exists(self, key: str) -> bool:
        if await self._local.exists(key):
            return True
        try:
            return await self._remote.exists(key)
        except SinkError:
            return False

    async def stat(self, key: str) -> ArtifactStat:
        try:
            return await self._local.stat(key)
        except FileNotFoundError:
            return await self._remote.stat(key)

    async def close(self) -> None:
        """Flush all pending mirrors then shut down."""
        if self._closed:
            return
        self._closed = True
        # Drain marker: worker will exit once the sentinel is reached.
        await self._queue.put(None)
        worker = self._worker
        if worker is not None:
            await worker
        # Close the remote sink so its client pool is released.
        try:
            await self._remote.close()
        except Exception as exc:
            logger.debug("BufferedSink remote.close raised: %s", exc)
        try:
            await self._local.close()
        except Exception as exc:
            logger.debug("BufferedSink local.close raised: %s", exc)


__all__ = [
    "BufferedSink",
    "BufferedSinkStats",
]
