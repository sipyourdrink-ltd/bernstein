"""Amazon S3 :class:`ArtifactSink` (optional extra).

Use ``pip install bernstein[s3]`` to install ``boto3``. When the SDK
is not installed the module still imports cleanly — instantiation is
where the error surfaces. This mirrors the sandbox-backend pattern in
:mod:`bernstein.core.sandbox.backends.e2b`.

Credentials resolve in this order:

1. Explicit constructor arguments (``access_key_id``, ``secret_access_key``,
   ``session_token``, ``region``, ``endpoint_url``).
2. Standard AWS environment variables (``AWS_ACCESS_KEY_ID``,
   ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``, ``AWS_REGION``).
3. boto3's default credential chain (instance profile, ``~/.aws``, IAM
   role, etc.).

boto3 is synchronous. Every operation is dispatched through
:func:`asyncio.to_thread` so the event loop is not blocked during
PUT/GET round-trips. ``durable=True`` maps to a blocking synchronous
PUT — the object store's ACK is the S3 analogue of a local fsync.
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


class S3Unavailable(RuntimeError):
    """Raised when the ``boto3`` SDK is not installed."""


def _import_boto3() -> Any:
    """Return the ``boto3`` module or raise :class:`S3Unavailable`.

    Kept as a helper so tests can monkeypatch the importer and so the
    top-level module import remains cheap when the extra is missing.
    """
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise S3Unavailable(
            "boto3 is not installed. Install the 's3' extra: `pip install bernstein[s3]`",
        ) from exc
    return cast(Any, boto3)


def _import_botocore_exceptions() -> Any:
    try:
        from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise S3Unavailable(
            "botocore is not installed. Install the 's3' extra: `pip install bernstein[s3]`",
        ) from exc
    return cast(Any, botocore_exceptions)


class S3ArtifactSink(ArtifactSink):
    """:class:`ArtifactSink` backed by Amazon S3 (or any S3-compatible API).

    Use this sink when artifacts need to survive the host machine. The
    object-store PUT request is acknowledged synchronously for
    ``durable=True`` writes, providing the equivalent of a local fsync
    for crash-recovery purposes.

    For Cloudflare R2, use the thin subclass
    :class:`~bernstein.core.storage.sinks.r2.R2ArtifactSink`, which
    applies R2-specific endpoint + credential conventions on top of
    this implementation.
    """

    name: str = "s3"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        """Create the sink.

        Args:
            bucket: S3 bucket. Falls back to ``BERNSTEIN_S3_BUCKET``.
            prefix: Logical prefix prepended to every key. Empty by
                default.
            region: AWS region. Falls back to ``AWS_REGION``.
            endpoint_url: Optional custom endpoint (LocalStack, R2,
                MinIO). Defaults to the AWS endpoint.
            access_key_id: Explicit access key. Falls back to
                ``AWS_ACCESS_KEY_ID``.
            secret_access_key: Explicit secret. Falls back to
                ``AWS_SECRET_ACCESS_KEY``.
            session_token: Temporary session token. Falls back to
                ``AWS_SESSION_TOKEN``.
            client_factory: Test seam: callable returning a boto3
                client. When provided, takes priority over every other
                argument — used in unit tests to inject a stubbed
                client without touching the real SDK.
        """
        self._bucket = bucket or os.environ.get("BERNSTEIN_S3_BUCKET") or ""
        self._prefix = prefix.strip("/")
        self._region = region or os.environ.get("AWS_REGION")
        self._endpoint_url = endpoint_url or os.environ.get("AWS_ENDPOINT_URL")
        self._access_key_id = access_key_id or os.environ.get("AWS_ACCESS_KEY_ID")
        self._secret_access_key = secret_access_key or os.environ.get(
            "AWS_SECRET_ACCESS_KEY",
        )
        self._session_token = session_token or os.environ.get("AWS_SESSION_TOKEN")
        self._client_factory = client_factory
        self._client: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def bucket(self) -> str:
        """Expose the configured bucket for diagnostics."""
        return self._bucket

    def _object_key(self, key: str) -> str:
        normalised = normalise_key(key)
        if self._prefix:
            return f"{self._prefix}/{normalised}"
        return normalised

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            # Validate configuration BEFORE importing boto3 so operators
            # hit the informative SinkError rather than S3Unavailable
            # when both are wrong.
            if not self._bucket:
                raise SinkError(
                    "S3 sink requires a bucket (constructor or BERNSTEIN_S3_BUCKET)",
                )
            if self._client_factory is not None:
                self._client = await asyncio.to_thread(self._client_factory)
                return self._client
            boto3 = _import_boto3()

            def _build() -> Any:
                kwargs: dict[str, Any] = {}
                if self._region:
                    kwargs["region_name"] = self._region
                if self._endpoint_url:
                    kwargs["endpoint_url"] = self._endpoint_url
                if self._access_key_id:
                    kwargs["aws_access_key_id"] = self._access_key_id
                if self._secret_access_key:
                    kwargs["aws_secret_access_key"] = self._secret_access_key
                if self._session_token:
                    kwargs["aws_session_token"] = self._session_token
                return boto3.client("s3", **kwargs)

            self._client = await asyncio.to_thread(_build)
            return self._client

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        durable: bool = True,
        content_type: str | None = None,
    ) -> None:
        del durable  # S3 PUT is always synchronously acknowledged
        client = await self._ensure_client()
        object_key = self._object_key(key)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            await asyncio.to_thread(client.put_object, **kwargs)
        except Exception as exc:  # pragma: no cover - exercised via integration
            raise SinkError(f"S3 PUT {object_key!r} failed: {exc}") from exc

    async def read(self, key: str) -> bytes:
        client = await self._ensure_client()
        object_key = self._object_key(key)
        exceptions = _import_botocore_exceptions()

        def _do_read() -> bytes:
            try:
                response = client.get_object(Bucket=self._bucket, Key=object_key)
            except exceptions.ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"NoSuchKey", "404"}:
                    raise FileNotFoundError(object_key) from exc
                raise SinkError(f"S3 GET {object_key!r} failed: {exc}") from exc
            body = response["Body"]
            try:
                return body.read()  # type: ignore[no-any-return]
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()

        return await asyncio.to_thread(_do_read)

    async def list(self, prefix: str) -> list[str]:
        client = await self._ensure_client()
        # ``prefix`` is a logical user-facing prefix; combine it with the
        # sink's own prefix to hit the S3 object store.
        logical_prefix = prefix.strip("/")
        s3_prefix_parts = [p for p in (self._prefix, logical_prefix) if p]
        s3_prefix = "/".join(s3_prefix_parts)

        def _do_list() -> list[str]:
            paginator = client.get_paginator("list_objects_v2")
            results: list[str] = []
            pages = paginator.paginate(Bucket=self._bucket, Prefix=s3_prefix)
            for page in pages:
                contents: list[dict[str, Any]] = page.get("Contents", []) or []
                for item in contents:
                    k = str(item["Key"])
                    if self._prefix and k.startswith(self._prefix + "/"):
                        k = k[len(self._prefix) + 1 :]
                    results.append(k)
            results.sort()
            return results

        return await asyncio.to_thread(_do_list)

    async def delete(self, key: str) -> None:
        client = await self._ensure_client()
        object_key = self._object_key(key)
        try:
            await asyncio.to_thread(
                client.delete_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except Exception as exc:  # pragma: no cover - exercised via integration
            raise SinkError(f"S3 DELETE {object_key!r} failed: {exc}") from exc

    async def exists(self, key: str) -> bool:
        client = await self._ensure_client()
        object_key = self._object_key(key)
        exceptions = _import_botocore_exceptions()

        def _do_head() -> bool:
            try:
                client.head_object(Bucket=self._bucket, Key=object_key)
            except exceptions.ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"NoSuchKey", "404", "NotFound"}:
                    return False
                raise SinkError(f"S3 HEAD {object_key!r} failed: {exc}") from exc
            return True

        return await asyncio.to_thread(_do_head)

    async def stat(self, key: str) -> ArtifactStat:
        client = await self._ensure_client()
        object_key = self._object_key(key)
        exceptions = _import_botocore_exceptions()

        def _do_head() -> ArtifactStat:
            try:
                resp = client.head_object(Bucket=self._bucket, Key=object_key)
            except exceptions.ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"NoSuchKey", "404", "NotFound"}:
                    raise FileNotFoundError(object_key) from exc
                raise SinkError(f"S3 HEAD {object_key!r} failed: {exc}") from exc
            last_modified = resp.get("LastModified")
            try:
                mtime = float(last_modified.timestamp()) if last_modified else 0.0
            except AttributeError:
                mtime = 0.0
            return ArtifactStat(
                size_bytes=int(resp.get("ContentLength", 0)),
                last_modified_unix=mtime,
                etag=str(resp.get("ETag", "")).strip('"') or None,
                content_type=resp.get("ContentType"),
            )

        return await asyncio.to_thread(_do_head)

    async def close(self) -> None:
        # boto3 clients use urllib3 connection pools internally; the
        # official guidance is to let them be garbage-collected. We
        # drop the reference here and let the normal lifecycle clean up.
        self._client = None


__all__ = [
    "S3ArtifactSink",
    "S3Unavailable",
]
