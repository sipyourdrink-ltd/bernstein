"""Local-filesystem :class:`ArtifactSink` — default, zero behaviour change.

This sink preserves Bernstein's pre-oai-003 behaviour: artifacts map
1:1 to files under a root directory (typically ``.sdd/``). It is the
default selected by plan.yaml when ``storage:`` is omitted so existing
deployments observe no diff.

Durability semantics match the atomic-write helpers in
:mod:`bernstein.core.persistence.atomic_write`: every ``durable=True``
write calls ``fsync`` on both the file descriptor and (on POSIX) the
parent directory, so a crash immediately after ``write`` returns
cannot leave a torn or missing artifact.

``durable=False`` still fsyncs — the local sink has no asynchronous
path so the distinction exists only for protocol parity with remote
sinks where it genuinely changes latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from bernstein.core.storage.sink import (
    ArtifactSink,
    ArtifactStat,
    normalise_key,
)

logger = logging.getLogger(__name__)


class LocalFsSink(ArtifactSink):
    """:class:`ArtifactSink` backed by a local directory.

    Every logical key is stored at ``root / key``. Keys are normalised
    through :func:`bernstein.core.storage.sink.normalise_key` so
    callers cannot escape ``root`` via ``..`` segments or absolute
    paths.

    The class is intentionally thin: the heavy lifting happens inside
    :mod:`bernstein.core.persistence.atomic_write`, which the sink
    re-uses to preserve the existing fsync semantics that the WAL and
    HMAC audit log depend on.
    """

    name: str = "local_fs"

    def __init__(self, root: Path | str | None = None) -> None:
        """Create a sink rooted at *root*.

        Args:
            root: Absolute or relative directory where artifacts land.
                Defaults to ``.sdd`` under the current working
                directory so constructing a sink without arguments
                matches the legacy layout. The directory is created on
                demand by the first write — read-only roots that do
                not yet exist remain unchanged.
        """
        self._root = Path(root) if root is not None else Path(".sdd")

    @property
    def root(self) -> Path:
        """Return the resolved on-disk root for diagnostics."""
        return self._root

    def _path_for(self, key: str) -> Path:
        return self._root / normalise_key(key)

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        """Write *data* to the file mapped from *key*.

        ``content_type`` is ignored — local filesystems have no generic
        slot for MIME metadata. The argument is accepted for protocol
        compliance.
        """
        del content_type  # unused on local FS; accepted for protocol parity
        path = self._path_for(key)

        def _sync_write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Import inside the function so importers that don't touch
            # ``write`` never pay for the atomic-write module cost.
            from bernstein.core.persistence.atomic_write import (
                write_atomic_bytes,
            )

            write_atomic_bytes(path, data)
            if not durable:
                # ``write_atomic_bytes`` already fsyncs; nothing extra
                # to do when durability was not requested. We still
                # return the file on disk because the local sink has
                # no cheaper "buffered" mode.
                return

        await asyncio.to_thread(_sync_write)

    async def read(self, key: str) -> bytes:
        path = self._path_for(key)

        def _sync_read() -> bytes:
            try:
                return path.read_bytes()
            except FileNotFoundError:
                raise
            except IsADirectoryError as exc:
                raise FileNotFoundError(str(path)) from exc

        return await asyncio.to_thread(_sync_read)

    async def list(self, prefix: str) -> list[str]:
        # ``normalise_key`` rejects empty strings; handle the "list
        # everything" case explicitly.
        prefix_norm = prefix.strip("/")
        search_root = self._root / prefix_norm if prefix_norm else self._root

        def _sync_list() -> list[str]:
            if not search_root.exists():
                return []
            results: list[str] = []
            if search_root.is_file():
                rel = search_root.relative_to(self._root)
                results.append(str(rel).replace(os.sep, "/"))
                return results
            for path in search_root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    rel = path.relative_to(self._root)
                except ValueError:
                    continue
                results.append(str(rel).replace(os.sep, "/"))
            results.sort()
            return results

        return await asyncio.to_thread(_sync_list)

    async def delete(self, key: str) -> None:
        path = self._path_for(key)

        def _sync_delete() -> None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

        await asyncio.to_thread(_sync_delete)

    async def exists(self, key: str) -> bool:
        path = self._path_for(key)
        return await asyncio.to_thread(path.is_file)

    async def stat(self, key: str) -> ArtifactStat:
        path = self._path_for(key)

        def _sync_stat() -> ArtifactStat:
            try:
                st = path.stat()
            except FileNotFoundError:
                raise
            except (OSError, NotADirectoryError) as exc:
                raise FileNotFoundError(str(path)) from exc
            # Treat directories as "not an artifact"; the sink
            # protocol is key/value and directories have no content.
            if not path.is_file():
                raise FileNotFoundError(str(path))
            return ArtifactStat(
                size_bytes=int(st.st_size),
                last_modified_unix=float(st.st_mtime),
                etag=None,
                content_type=None,
            )

        return await asyncio.to_thread(_sync_stat)

    async def close(self) -> None:  # pragma: no cover - nothing to release
        """No-op: local sink owns no client handles."""
        return None


__all__ = ["LocalFsSink"]
