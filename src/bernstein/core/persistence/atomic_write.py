"""Atomic file write helpers for crash-safe persistence.

Writing runtime state via the naive ``path.write_text(...)`` pattern is
non-atomic: ``open(path, 'w')`` truncates the target before any bytes are
written, and a crash (SIGKILL, power loss) between the ``open`` and the
final flush leaves an empty or half-written file that downstream loaders
will misinterpret or fail on.

This module centralises the crash-safe ``temp + fsync + os.replace``
pattern for all ``.sdd/runtime/`` writes. POSIX and Windows both
guarantee that :func:`os.replace` swaps the directory entry atomically,
so readers see either the old contents or the new contents - never a
torn mix.

Typical usage::

    from bernstein.core.persistence.atomic_write import (
        write_atomic_bytes,
        write_atomic_json,
        write_atomic_text,
    )

    write_atomic_json(path, payload)

Append-only JSONL files (e.g. ``replay.jsonl``, ``wal.jsonl``) should
continue to use direct appends with ``fsync`` - per-line atomicity is
provided by the OS at write boundaries below ``PIPE_BUF`` and the
on-disk state is already crash-safe at line granularity.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _tmp_path_for(path: Path) -> Path:
    """Build a unique sibling temp path for *path*.

    Incorporating both the PID and ``os.urandom`` disambiguates concurrent
    writers and interrupted previous runs so we never collide on the tmp
    slot.
    """
    suffix = f".tmp.{os.getpid()}.{os.urandom(4).hex()}"
    return path.with_name(path.name + suffix)


def _fsync_dir(directory: Path) -> None:
    """Fsync *directory* so the rename is durable on POSIX.

    Windows does not expose directory fsync; the call is skipped there.
    Best-effort: failures are logged at debug level only - the rename
    itself is already atomic from the perspective of concurrent readers.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError as exc:
        logger.debug("Cannot open directory %s for fsync: %s", directory, exc)
        return
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            logger.debug("Cannot fsync directory %s: %s", directory, exc)
    finally:
        os.close(fd)


def write_atomic_bytes(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path* via temp-file + ``os.replace``.

    The sequence is:

    1. Create parent directory if missing.
    2. Write bytes to ``<path>.tmp.<pid>.<rand>`` and ``fsync`` the fd.
    3. ``os.replace`` the temp file onto *path* (atomic rename).
    4. ``fsync`` the containing directory so the rename is durable.

    On any error the temp file is unlinked so the filesystem does not
    accumulate stale ``.tmp.*`` entries.

    Args:
        path: Final destination file.
        data: Byte payload to write.

    Raises:
        OSError: Propagated from the underlying filesystem calls after
            the temp file is cleaned up. Callers that prefer silent
            best-effort writes should catch this at the call site.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(path)
    try:
        # 0o600 - owner-only. Runtime state may carry task metadata, session
        # tokens, or other material that should not be world-readable.
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(path.parent)


def write_atomic_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """Atomically write *data* as text to *path*.

    Thin wrapper over :func:`write_atomic_bytes` that handles encoding.

    Args:
        path: Final destination file.
        data: Text payload to write.
        encoding: Text encoding (default UTF-8).
    """
    write_atomic_bytes(path, data.encode(encoding))


def promote_atomic(staged_path: Path, path: Path) -> None:
    """Atomically move an already-written staged file onto *path*.

    Companion to :func:`write_atomic_bytes` for producers that emit a
    *set* of files which must become visible together: write every file
    of the set into a scratch directory on the same filesystem with
    :func:`write_atomic_text` / :func:`write_atomic_bytes`, and only once
    the whole set is durable call this for each file. A failure at any
    point before the first promote leaves the published location exactly
    as it was, so a consumer reading it sees the previous complete set.

    ``os.replace`` swaps the directory entry, so a reader holding *path*
    sees either the previous file or the new one - never a truncated or
    half-written mix.

    Args:
        staged_path: Fully written source file. Must live on the same
            filesystem as *path*, otherwise ``os.replace`` raises.
        path: Final destination. Parent directory is created if missing.

    Raises:
        OSError: Propagated from ``os.replace`` (e.g. a cross-device
            staging path, or a destination that is a directory).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(staged_path), str(path))
    _fsync_dir(path.parent)


def write_atomic_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    default: Any = None,
) -> None:
    """Atomically serialise *payload* as JSON to *path*.

    Args:
        path: Final destination file.
        payload: JSON-serialisable value.
        indent: JSON indent (``None`` for compact output).
        sort_keys: Pass through to :func:`json.dumps`.
        default: Pass through to :func:`json.dumps` for non-serialisable
            fallbacks (e.g. ``default=str``).
    """
    encoded = json.dumps(payload, indent=indent, sort_keys=sort_keys, default=default)
    write_atomic_text(path, encoded)


__all__ = [
    "promote_atomic",
    "write_atomic_bytes",
    "write_atomic_json",
    "write_atomic_text",
]
