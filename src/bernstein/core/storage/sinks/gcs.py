"""Google Cloud Storage :class:`ArtifactSink` (optional extra).

Install with ``pip install bernstein[gcs]``. When the
``google-cloud-storage`` SDK is missing the module still imports
cleanly — instantiation is where the error surfaces.

Credentials resolve via the standard
``GOOGLE_APPLICATION_CREDENTIALS`` env var (service-account JSON) or
workload identity when running on GCP. Explicit project and
credential overrides are accepted via the constructor for unusual
deployments.

The google-cloud-storage client is synchronous; every operation runs
through :func:`asyncio.to_thread` to keep the event loop free.
``durable=True`` maps to a blocking synchronous upload — GCS's ACK is
the equivalent of a local fsync.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, cast

from bernstein.core.storage.sink import (
    ArtifactSink,
    ArtifactStat,
    SinkError,
    normalise_key,
)

logger = logging.getLogger(__name__)


class GCSUnavailable(RuntimeError):
    """Raised when ``google-cloud-storage`` is not installed."""


def _import_storage() -> Any:
    """Return the ``google.cloud.storage`` module or raise."""
    try:
        from google.cloud import storage  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:  # pragma: no cover - monkeypatched in tests
        raise GCSUnavailable(
            "google-cloud-storage is not installed. Install the 'gcs' extra: `pip install bernstein[gcs]`",
        ) from exc
    return cast(Any, storage)


def _import_gcs_exceptions() -> Any:
    try:
        from google.api_core import exceptions as api_exceptions  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise GCSUnavailable(
            "google-api-core is not installed (ships with google-cloud-storage)",
        ) from exc
    return cast(Any, api_exceptions)


class GCSArtifactSink(ArtifactSink):
    """:class:`ArtifactSink` backed by Google Cloud Storage."""

    name: str = "gcs"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str = "",
        project: str | None = None,
        credentials_path: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        """Create the sink.

        Args:
            bucket: GCS bucket. Falls back to ``BERNSTEIN_GCS_BUCKET``.
            prefix: Logical prefix prepended to every key.
            project: GCP project. Falls back to ``GOOGLE_CLOUD_PROJECT``.
            credentials_path: Override
                ``GOOGLE_APPLICATION_CREDENTIALS`` for this sink.
            client_factory: Test seam returning a pre-built client.
        """
        self._bucket = bucket or os.environ.get("BERNSTEIN_GCS_BUCKET") or ""
        self._prefix = prefix.strip("/")
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if credentials_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", credentials_path)
        self._client_factory = client_factory
        self._client: Any | None = None
        self._bucket_handle: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def bucket(self) -> str:
        """Expose the configured bucket for diagnostics."""
        return self._bucket

    def _object_name(self, key: str) -> str:
        normalised = normalise_key(key)
        if self._prefix:
            return f"{self._prefix}/{normalised}"
        return normalised

    async def _ensure_bucket(self) -> Any:
        if self._bucket_handle is not None:
            return self._bucket_handle
        async with self._lock:
            if self._bucket_handle is not None:
                return self._bucket_handle
            if self._client_factory is not None:
                self._client = await asyncio.to_thread(self._client_factory)
            else:
                if not self._bucket:
                    raise SinkError(
                        "GCS sink requires a bucket (constructor or BERNSTEIN_GCS_BUCKET)",
                    )
                storage = _import_storage()
                self._client = await asyncio.to_thread(
                    storage.Client,
                    project=self._project,
                )
            client = self._client
            assert client is not None, "GCS client must be initialised"
            self._bucket_handle = await asyncio.to_thread(
                client.bucket,
                self._bucket,
            )
            return self._bucket_handle

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        del durable  # GCS upload is synchronously acknowledged
        bucket = await self._ensure_bucket()
        object_name = self._object_name(key)

        def _do_write() -> None:
            blob = bucket.blob(object_name)
            try:
                blob.upload_from_string(
                    data,
                    content_type=content_type,
                )
            except Exception as exc:
                raise SinkError(f"GCS upload {object_name!r} failed: {exc}") from exc

        await asyncio.to_thread(_do_write)

    async def read(self, key: str) -> bytes:
        bucket = await self._ensure_bucket()
        object_name = self._object_name(key)
        exceptions = _import_gcs_exceptions()

        def _do_read() -> bytes:
            blob = bucket.blob(object_name)
            try:
                return blob.download_as_bytes()  # type: ignore[no-any-return]
            except exceptions.NotFound as exc:
                raise FileNotFoundError(object_name) from exc
            except Exception as exc:
                raise SinkError(f"GCS download {object_name!r} failed: {exc}") from exc

        return await asyncio.to_thread(_do_read)

    async def list(self, prefix: str) -> list[str]:
        bucket = await self._ensure_bucket()
        logical_prefix = prefix.strip("/")
        combined = "/".join(p for p in (self._prefix, logical_prefix) if p)

        def _do_list() -> list[str]:
            names: list[str] = []
            for blob in bucket.list_blobs(prefix=combined):
                k = str(blob.name)
                if self._prefix and k.startswith(self._prefix + "/"):
                    k = k[len(self._prefix) + 1 :]
                names.append(k)
            names.sort()
            return names

        return await asyncio.to_thread(_do_list)

    async def delete(self, key: str) -> None:
        bucket = await self._ensure_bucket()
        object_name = self._object_name(key)
        exceptions = _import_gcs_exceptions()

        def _do_delete() -> None:
            blob = bucket.blob(object_name)
            try:
                blob.delete()
            except exceptions.NotFound:
                return
            except Exception as exc:
                raise SinkError(f"GCS delete {object_name!r} failed: {exc}") from exc

        await asyncio.to_thread(_do_delete)

    async def exists(self, key: str) -> bool:
        bucket = await self._ensure_bucket()
        object_name = self._object_name(key)

        def _do_exists() -> bool:
            blob = bucket.blob(object_name)
            try:
                return bool(blob.exists())
            except Exception as exc:
                raise SinkError(f"GCS exists {object_name!r} failed: {exc}") from exc

        return await asyncio.to_thread(_do_exists)

    async def stat(self, key: str) -> ArtifactStat:
        bucket = await self._ensure_bucket()
        object_name = self._object_name(key)
        exceptions = _import_gcs_exceptions()

        def _do_stat() -> ArtifactStat:
            blob = bucket.blob(object_name)
            try:
                blob.reload()
            except exceptions.NotFound as exc:
                raise FileNotFoundError(object_name) from exc
            updated = blob.updated
            mtime = float(updated.timestamp()) if updated else 0.0
            return ArtifactStat(
                size_bytes=int(blob.size or 0),
                last_modified_unix=mtime,
                etag=str(blob.etag) if blob.etag else None,
                content_type=blob.content_type,
            )

        return await asyncio.to_thread(_do_stat)

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._bucket_handle = None
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                await asyncio.to_thread(close)


__all__ = [
    "GCSArtifactSink",
    "GCSUnavailable",
]
