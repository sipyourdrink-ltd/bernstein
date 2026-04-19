"""Azure Blob Storage :class:`ArtifactSink` (optional extra).

Install with ``pip install bernstein[azure]`` to pull in
``azure-storage-blob``. When the SDK is not installed the module
still imports cleanly — instantiation is where the error surfaces.

Credential handling:

- ``AZURE_STORAGE_CONNECTION_STRING`` — preferred; full connection string.
- ``AZURE_STORAGE_ACCOUNT_NAME`` + ``AZURE_STORAGE_ACCOUNT_KEY`` — classic
  account key auth.
- Constructor overrides take priority over environment variables.

The ``BlobServiceClient`` is synchronous (there is an async Azure SDK
but using it here would add an unconditional dependency for non-Azure
users via transitive async-http packages). Operations are dispatched
through :func:`asyncio.to_thread` so the event loop is not blocked
during upload/download round-trips.
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


class AzureBlobUnavailable(RuntimeError):
    """Raised when the ``azure-storage-blob`` SDK is not installed."""


def _import_blob_sdk() -> Any:
    try:
        from azure.storage import blob  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise AzureBlobUnavailable(
            "azure-storage-blob is not installed. Install the 'azure' extra: `pip install bernstein[azure]`",
        ) from exc
    return cast(Any, blob)


def _import_azure_core_exceptions() -> Any:
    try:
        from azure.core import exceptions  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise AzureBlobUnavailable(
            "azure-core is not installed (ships with azure-storage-blob)",
        ) from exc
    return cast(Any, exceptions)


class AzureBlobArtifactSink(ArtifactSink):
    """:class:`ArtifactSink` backed by Azure Blob Storage."""

    name: str = "azure_blob"

    def __init__(
        self,
        *,
        container: str | None = None,
        prefix: str = "",
        connection_string: str | None = None,
        account_name: str | None = None,
        account_key: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        """Create the sink.

        Args:
            container: Blob container name. Falls back to
                ``BERNSTEIN_AZURE_CONTAINER``.
            prefix: Logical prefix prepended to every key.
            connection_string: Full Azure connection string. Falls
                back to ``AZURE_STORAGE_CONNECTION_STRING``.
            account_name: Storage account name. Falls back to
                ``AZURE_STORAGE_ACCOUNT_NAME``.
            account_key: Storage account key. Falls back to
                ``AZURE_STORAGE_ACCOUNT_KEY``.
            client_factory: Test seam returning a ``BlobServiceClient``.
        """
        self._container = container or os.environ.get("BERNSTEIN_AZURE_CONTAINER") or ""
        self._prefix = prefix.strip("/")
        self._connection_string = connection_string or os.environ.get(
            "AZURE_STORAGE_CONNECTION_STRING",
        )
        self._account_name = account_name or os.environ.get(
            "AZURE_STORAGE_ACCOUNT_NAME",
        )
        self._account_key = account_key or os.environ.get(
            "AZURE_STORAGE_ACCOUNT_KEY",
        )
        self._client_factory = client_factory
        self._service_client: Any | None = None
        self._container_client: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def container(self) -> str:
        """Expose the configured container for diagnostics."""
        return self._container

    def _blob_name(self, key: str) -> str:
        normalised = normalise_key(key)
        if self._prefix:
            return f"{self._prefix}/{normalised}"
        return normalised

    async def _ensure_container(self) -> Any:
        if self._container_client is not None:
            return self._container_client
        async with self._lock:
            if self._container_client is not None:
                return self._container_client
            if self._client_factory is not None:
                self._service_client = await asyncio.to_thread(self._client_factory)
            else:
                if not self._container:
                    raise SinkError(
                        "Azure Blob sink requires a container (constructor or BERNSTEIN_AZURE_CONTAINER)",
                    )
                blob_sdk = _import_blob_sdk()
                service_cls = blob_sdk.BlobServiceClient

                def _build() -> Any:
                    if self._connection_string:
                        return service_cls.from_connection_string(self._connection_string)
                    if self._account_name and self._account_key:
                        account_url = f"https://{self._account_name}.blob.core.windows.net"
                        return service_cls(
                            account_url=account_url,
                            credential=self._account_key,
                        )
                    raise SinkError(
                        "Azure Blob sink requires AZURE_STORAGE_CONNECTION_STRING "
                        "or AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY",
                    )

                self._service_client = await asyncio.to_thread(_build)
            service = self._service_client
            assert service is not None, "service client must be initialised"
            self._container_client = await asyncio.to_thread(
                service.get_container_client,
                self._container,
            )
            return self._container_client

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        del durable  # PUT is synchronously acknowledged
        container = await self._ensure_container()
        blob_name = self._blob_name(key)
        blob_sdk = _import_blob_sdk()
        content_settings_cls = getattr(blob_sdk, "ContentSettings", None)

        def _do_write() -> None:
            blob_client = container.get_blob_client(blob_name)
            kwargs: dict[str, Any] = {"overwrite": True}
            if content_type and content_settings_cls is not None:
                kwargs["content_settings"] = content_settings_cls(
                    content_type=content_type,
                )
            try:
                blob_client.upload_blob(data, **kwargs)
            except Exception as exc:
                raise SinkError(f"Azure upload {blob_name!r} failed: {exc}") from exc

        await asyncio.to_thread(_do_write)

    async def read(self, key: str) -> bytes:
        container = await self._ensure_container()
        blob_name = self._blob_name(key)
        exceptions = _import_azure_core_exceptions()

        def _do_read() -> bytes:
            blob_client = container.get_blob_client(blob_name)
            try:
                downloader = blob_client.download_blob()
                return downloader.readall()  # type: ignore[no-any-return]
            except exceptions.ResourceNotFoundError as exc:
                raise FileNotFoundError(blob_name) from exc
            except Exception as exc:
                raise SinkError(f"Azure download {blob_name!r} failed: {exc}") from exc

        return await asyncio.to_thread(_do_read)

    async def list(self, prefix: str) -> list[str]:
        container = await self._ensure_container()
        logical_prefix = prefix.strip("/")
        combined = "/".join(p for p in (self._prefix, logical_prefix) if p)

        def _do_list() -> list[str]:
            names: list[str] = []
            for blob in container.list_blobs(name_starts_with=combined):
                k = str(blob.name)
                if self._prefix and k.startswith(self._prefix + "/"):
                    k = k[len(self._prefix) + 1 :]
                names.append(k)
            names.sort()
            return names

        return await asyncio.to_thread(_do_list)

    async def delete(self, key: str) -> None:
        container = await self._ensure_container()
        blob_name = self._blob_name(key)
        exceptions = _import_azure_core_exceptions()

        def _do_delete() -> None:
            blob_client = container.get_blob_client(blob_name)
            try:
                blob_client.delete_blob()
            except exceptions.ResourceNotFoundError:
                return
            except Exception as exc:
                raise SinkError(f"Azure delete {blob_name!r} failed: {exc}") from exc

        await asyncio.to_thread(_do_delete)

    async def exists(self, key: str) -> bool:
        container = await self._ensure_container()
        blob_name = self._blob_name(key)

        def _do_exists() -> bool:
            blob_client = container.get_blob_client(blob_name)
            try:
                return bool(blob_client.exists())
            except Exception as exc:
                raise SinkError(f"Azure exists {blob_name!r} failed: {exc}") from exc

        return await asyncio.to_thread(_do_exists)

    async def stat(self, key: str) -> ArtifactStat:
        container = await self._ensure_container()
        blob_name = self._blob_name(key)
        exceptions = _import_azure_core_exceptions()

        def _do_stat() -> ArtifactStat:
            blob_client = container.get_blob_client(blob_name)
            try:
                props = blob_client.get_blob_properties()
            except exceptions.ResourceNotFoundError as exc:
                raise FileNotFoundError(blob_name) from exc
            except Exception as exc:
                raise SinkError(f"Azure stat {blob_name!r} failed: {exc}") from exc
            last_modified = getattr(props, "last_modified", None)
            mtime = float(last_modified.timestamp()) if last_modified else 0.0
            content_settings = getattr(props, "content_settings", None)
            content_type = getattr(content_settings, "content_type", None) if content_settings is not None else None
            return ArtifactStat(
                size_bytes=int(getattr(props, "size", 0) or 0),
                last_modified_unix=mtime,
                etag=str(getattr(props, "etag", "") or "").strip('"') or None,
                content_type=content_type,
            )

        return await asyncio.to_thread(_do_stat)

    async def close(self) -> None:
        container = self._container_client
        service = self._service_client
        self._container_client = None
        self._service_client = None
        for client in (container, service):
            if client is None:
                continue
            close = getattr(client, "close", None)
            if close is not None:
                await asyncio.to_thread(close)


__all__ = [
    "AzureBlobArtifactSink",
    "AzureBlobUnavailable",
]
