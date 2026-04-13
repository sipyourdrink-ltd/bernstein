"""Content-addressable storage for agent outputs.

Stores arbitrary content (files, text, structured data) keyed by SHA-256
digest.  Duplicate content is stored only once, yielding automatic
deduplication across agents and runs.

Storage layout mirrors git's object store::

    .sdd/cas/{first-2-hex-chars}/{full-sha256-hex}

Each blob is accompanied by a ``.meta.json`` sidecar containing the
:class:`CASEntry` fields (content_type, size, timestamps, user metadata).

Typical usage::

    store = CASStore(Path(".sdd/cas"))
    digest = store.put(b"hello world", content_type="text/plain")
    assert store.get(digest) == b"hello world"
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CASEntry:
    """Immutable descriptor for a content-addressable blob.

    Attributes:
        digest: SHA-256 hex digest of the stored content.
        size_bytes: Content length in bytes.
        created_at: Unix timestamp of first insertion.
        content_type: MIME-style content type (e.g. ``"text/plain"``).
        metadata: Arbitrary user-supplied metadata.
    """

    digest: str
    size_bytes: int
    created_at: float
    content_type: str
    metadata: dict[str, Any] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]


# ---------------------------------------------------------------------------
# Store statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CASStats:
    """Aggregate statistics for a :class:`CASStore`.

    Attributes:
        total_entries: Number of distinct blobs stored.
        total_bytes: Sum of all blob sizes.
        dedup_saves: Number of :meth:`CASStore.put` calls that hit an
            existing blob (content already stored).
    """

    total_entries: int
    total_bytes: int
    dedup_saves: int


# ---------------------------------------------------------------------------
# CASStore
# ---------------------------------------------------------------------------


class CASStore:
    """Content-addressable blob store backed by the local filesystem.

    Args:
        store_dir: Root directory for blob storage (typically ``.sdd/cas/``).
    """

    def __init__(self, store_dir: Path) -> None:
        self._root = store_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._dedup_saves = 0

    # -- internal helpers ----------------------------------------------------

    _HEX_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")

    @staticmethod
    def _digest(content: bytes) -> str:
        """Compute the SHA-256 hex digest of *content*."""
        return hashlib.sha256(content).hexdigest()

    def _validate_digest(self, digest: str) -> None:
        """Ensure *digest* is a valid SHA-256 hex string (no path traversal)."""
        if not self._HEX_RE.match(digest):
            msg = f"Invalid CAS digest (expected 64 hex chars): {digest!r}"
            raise ValueError(msg)

    def _shard_dir(self, digest: str) -> Path:
        """Return the shard directory for *digest* (first two hex chars)."""
        return self._root / digest[:2]

    def _blob_path(self, digest: str) -> Path:
        """Return the filesystem path for the blob identified by *digest*."""
        return self._shard_dir(digest) / digest

    def _meta_path(self, digest: str) -> Path:
        """Return the filesystem path for the sidecar metadata file."""
        return self._shard_dir(digest) / f"{digest}.meta.json"

    def _write_meta(self, entry: CASEntry) -> None:
        """Persist a :class:`CASEntry` as a JSON sidecar."""
        self._meta_path(entry.digest).write_text(
            json.dumps(asdict(entry), indent=2) + "\n",
        )

    def _read_meta(self, digest: str) -> CASEntry | None:
        """Load the :class:`CASEntry` sidecar, or ``None`` if missing."""
        meta = self._meta_path(digest)
        if not meta.exists():
            return None
        data: dict[str, Any] = json.loads(meta.read_text())
        return CASEntry(**data)

    # -- public API ----------------------------------------------------------

    def put(
        self,
        content: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store *content* and return its SHA-256 digest.

        If the digest already exists on disk the write is skipped
        (deduplication).

        Args:
            content: Raw bytes to store.
            content_type: MIME-style content type.
            metadata: Optional user-supplied metadata dict.

        Returns:
            The SHA-256 hex digest string.
        """
        digest = self._digest(content)

        if self.has(digest):
            self._dedup_saves += 1
            logger.debug("CAS dedup hit: %s", digest[:12])
            return digest

        shard = self._shard_dir(digest)
        shard.mkdir(parents=True, exist_ok=True)

        self._blob_path(digest).write_bytes(content)

        entry = CASEntry(
            digest=digest,
            size_bytes=len(content),
            created_at=time.time(),
            content_type=content_type,
            metadata=metadata or {},
        )
        self._write_meta(entry)

        logger.debug("CAS stored %d bytes as %s", len(content), digest[:12])
        return digest

    def get(self, digest: str) -> bytes | None:
        """Retrieve stored content by *digest*, or ``None`` if absent.

        Args:
            digest: SHA-256 hex digest returned by :meth:`put`.

        Returns:
            The stored bytes, or ``None`` when the digest is unknown.

        Raises:
            ValueError: If *digest* is not a valid 64-char hex string.
        """
        self._validate_digest(digest)
        blob = self._blob_path(digest)
        if not blob.exists():
            return None
        return blob.read_bytes()

    def has(self, digest: str) -> bool:
        """Check whether *digest* exists in the store.

        Args:
            digest: SHA-256 hex digest to look up.

        Returns:
            ``True`` if the blob and its metadata are both present.

        Raises:
            ValueError: If *digest* is not a valid 64-char hex string.
        """
        self._validate_digest(digest)
        return self._blob_path(digest).exists() and self._meta_path(digest).exists()

    def delete(self, digest: str) -> bool:
        """Remove the blob and metadata identified by *digest*.

        Args:
            digest: SHA-256 hex digest of the entry to remove.

        Returns:
            ``True`` if the entry was found and deleted, ``False`` otherwise.

        Raises:
            ValueError: If *digest* is not a valid 64-char hex string.
        """
        self._validate_digest(digest)
        blob = self._blob_path(digest)
        meta = self._meta_path(digest)

        if not blob.exists():
            return False

        blob.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

        # Remove shard directory if empty.
        shard = self._shard_dir(digest)
        with contextlib.suppress(OSError):
            shard.rmdir()

        logger.debug("CAS deleted %s", digest[:12])
        return True

    def get_entry(self, digest: str) -> CASEntry | None:
        """Return the :class:`CASEntry` metadata for *digest*, or ``None``.

        Args:
            digest: SHA-256 hex digest to look up.

        Returns:
            The entry descriptor, or ``None`` when the digest is unknown.
        """
        return self._read_meta(digest)

    def list_entries(self) -> list[CASEntry]:
        """List all stored entries.

        Returns:
            A list of :class:`CASEntry` objects sorted by creation time
            (oldest first).
        """
        entries: list[CASEntry] = []
        if not self._root.exists():
            return entries

        for shard in sorted(self._root.iterdir()):
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            for meta_file in sorted(shard.glob("*.meta.json")):
                try:
                    data: dict[str, Any] = json.loads(meta_file.read_text())
                    entries.append(CASEntry(**data))
                except (json.JSONDecodeError, TypeError, KeyError):
                    logger.warning("Corrupt CAS metadata: %s", meta_file)

        entries.sort(key=lambda e: e.created_at)
        return entries

    def stats(self) -> CASStats:
        """Compute aggregate statistics for this store.

        Returns:
            A :class:`CASStats` with total entries, total bytes, and the
            number of deduplicated ``put`` calls in this session.
        """
        entries = self.list_entries()
        return CASStats(
            total_entries=len(entries),
            total_bytes=sum(e.size_bytes for e in entries),
            dedup_saves=self._dedup_saves,
        )

    @property
    def root(self) -> Path:
        """The root directory of this store."""
        return self._root


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def put_file(
    store: CASStore,
    file_path: Path,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Store the contents of *file_path* in *store*.

    The file name is automatically recorded in the metadata under the
    ``"source_file"`` key.

    Args:
        store: Target :class:`CASStore`.
        file_path: Path to the file to ingest.
        metadata: Additional metadata to attach.

    Returns:
        The SHA-256 hex digest of the stored content.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    content = file_path.read_bytes()
    merged: dict[str, Any] = {"source_file": str(file_path)}
    if metadata:
        merged.update(metadata)

    # Guess content type from suffix.
    suffix = file_path.suffix.lower()
    type_map: dict[str, str] = {
        ".py": "text/x-python",
        ".json": "application/json",
        ".yaml": "application/x-yaml",
        ".yml": "application/x-yaml",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".ts": "application/typescript",
    }
    content_type = type_map.get(suffix, "application/octet-stream")

    return store.put(content, content_type=content_type, metadata=merged)


def put_text(
    store: CASStore,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Store UTF-8 *text* in *store* with ``text/plain`` content type.

    Args:
        store: Target :class:`CASStore`.
        text: Text content to store.
        metadata: Additional metadata to attach.

    Returns:
        The SHA-256 hex digest of the stored content.
    """
    return store.put(
        text.encode("utf-8"),
        content_type="text/plain",
        metadata=metadata or {},
    )
