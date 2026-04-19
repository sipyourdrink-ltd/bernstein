"""Pluggable artifact sink protocol for ``.sdd/`` persistence (oai-003).

Bernstein writes runtime state, the WAL, HMAC audit logs, task outputs,
and metrics dumps under ``.sdd/`` on the orchestrator host. On ephemeral
compute (CI runners, Kubernetes pods, cloud sandboxes) that host can
disappear between orchestrator restarts, taking recovery state with it.

The :class:`ArtifactSink` protocol decouples the persistence layer from
the local filesystem so artifacts can stream to S3, GCS, Azure Blob,
Cloudflare R2, or any custom plugin. The default behaviour is preserved
by :class:`~bernstein.core.storage.sinks.local_fs.LocalFsSink`, which
simply reuses the on-disk layout. Remote sinks are optional extras.

Sinks are discovered via the ``bernstein.storage_sinks`` pluggy entry
point group (see :mod:`bernstein.core.storage.registry`).

The protocol is intentionally narrow: callers interact with logical
keys such as ``runtime/wal/run-123.wal.jsonl`` rather than file paths,
and every operation is asynchronous so backends can pipeline I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class ArtifactStat:
    """Metadata for a stored artifact.

    Attributes:
        size_bytes: Length of the artifact in bytes.
        last_modified_unix: Last-modified time as a Unix timestamp. For
            providers that do not expose sub-second resolution this is
            truncated to seconds.
        etag: Provider-specific entity tag (e.g. S3 ETag). ``None`` when
            the sink cannot supply one.
        content_type: MIME content type if the sink recorded one.
    """

    size_bytes: int
    last_modified_unix: float
    etag: str | None = None
    content_type: str | None = None


class SinkError(RuntimeError):
    """Base class for sink-level exceptions.

    Sink implementations should raise ``FileNotFoundError`` for a missing
    key (matching the local-filesystem semantics the WAL and audit log
    already depend on) and :class:`SinkError` for transient or protocol
    errors so callers can layer retries or circuit breakers.
    """


@runtime_checkable
class ArtifactSink(Protocol):
    """Pluggable persistence backend for ``.sdd/`` artifacts.

    Every sink implementation provides a minimal set of async key-value
    operations. Keys are forward-slash-delimited logical paths; sink
    implementations map them to whatever native addressing the backend
    supports (object store keys, filesystem paths, ...).

    Attributes:
        name: Canonical sink name used in plan.yaml ``storage.sink``
            (``local_fs``, ``s3``, ``gcs`` ...). Must be stable across
            versions.
    """

    name: str

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        """Write *data* at *key*.

        Args:
            key: Logical key such as ``runtime/wal/run-123.wal.jsonl``.
            data: Raw payload bytes.
            durable: When ``True``, the write must be observably durable
                before ``write`` returns (local ``fsync`` for LocalFs,
                synchronous PUT for S3-style backends). The WAL relies
                on this guarantee for crash safety. When ``False`` the
                backend MAY buffer the write for later flush.
            content_type: Optional MIME type hint. Sinks that support
                metadata store it verbatim; others ignore it.

        Raises:
            SinkError: For transient or protocol-level failures.
        """
        ...

    async def read(self, key: str) -> bytes:
        """Read the artifact stored at *key*.

        Raises:
            FileNotFoundError: If no artifact exists at *key*.
            SinkError: For transient or protocol-level failures.
        """
        ...

    async def list(self, prefix: str) -> list[str]:
        """List keys whose identifier starts with *prefix*.

        The returned list is sorted lexicographically. Implementations
        must internally paginate so callers get the full set in a
        single call.

        Args:
            prefix: Prefix to filter by. Pass ``""`` to list every key.

        Returns:
            Sorted list of matching keys.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove *key* from the sink.

        Idempotent: deleting a missing key is not an error.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Return True when *key* is present in the sink."""
        ...

    async def stat(self, key: str) -> ArtifactStat:
        """Return size/etag/modified metadata for *key*.

        Raises:
            FileNotFoundError: If no artifact exists at *key*.
        """
        ...

    async def close(self) -> None:
        """Flush pending writes and release any client resources.

        Safe to call multiple times. After ``close`` returns, callers
        must not invoke further operations on the sink.
        """
        ...


def normalise_key(key: str) -> str:
    """Canonicalise a sink key.

    - Strips leading slashes so callers can safely pass absolute-looking
      paths like ``/runtime/wal/run-1.jsonl``.
    - Rejects empty keys and ``.``/``..`` segments to prevent sinks
      from accidentally writing outside their logical root.

    Raises:
        ValueError: When the key cannot be canonicalised.
    """
    if not key:
        raise ValueError("sink key must be non-empty")
    stripped = key.lstrip("/")
    if not stripped:
        raise ValueError("sink key must contain at least one non-slash character")
    segments: list[str] = []
    for segment in stripped.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"sink key {key!r} contains forbidden segment {segment!r}")
        segments.append(segment)
    return "/".join(segments)


def join_keys(*parts: str) -> str:
    """Join sink key *parts* with forward slashes.

    Skips empty components so the result never contains ``//``. Raises
    :class:`ValueError` when every part is empty.
    """
    kept: Iterable[str] = (p.strip("/") for p in parts)
    cleaned = [segment for segment in kept if segment]
    if not cleaned:
        raise ValueError("at least one non-empty key part is required")
    return "/".join(cleaned)


__all__ = [
    "ArtifactSink",
    "ArtifactStat",
    "SinkError",
    "join_keys",
    "normalise_key",
]
