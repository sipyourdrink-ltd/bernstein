"""Cloudflare R2 :class:`ArtifactSink` (optional extra).

Cloudflare R2 implements the S3 API, so this sink is a thin subclass
of :class:`~bernstein.core.storage.sinks.s3.S3ArtifactSink` that wires
R2-specific credential env vars and constructs the per-account endpoint
URL.

Install with ``pip install bernstein[r2]`` (which pulls boto3 — the
same SDK used by :mod:`bernstein.core.storage.sinks.s3`).

Credentials:

- ``R2_ACCOUNT_ID`` — required; determines the endpoint URL.
- ``R2_ACCESS_KEY_ID`` — preferred; falls back to ``AWS_ACCESS_KEY_ID``.
- ``R2_SECRET_ACCESS_KEY`` — preferred; falls back to
  ``AWS_SECRET_ACCESS_KEY``.

Explicit constructor arguments take priority over environment
variables. Region is fixed to ``auto`` because R2 is a global
service; callers rarely need to override it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from bernstein.core.storage.sinks.s3 import S3ArtifactSink

logger = logging.getLogger(__name__)


class R2ArtifactSink(S3ArtifactSink):
    """Cloudflare R2 sink, implemented on top of the S3 client."""

    name: str = "r2"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str = "",
        account_id: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        endpoint_url: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        """Create the sink.

        Args:
            bucket: R2 bucket. Falls back to ``BERNSTEIN_R2_BUCKET``.
            prefix: Logical prefix prepended to every key.
            account_id: Cloudflare account ID. Falls back to
                ``R2_ACCOUNT_ID``.
            access_key_id: R2 access key. Falls back to
                ``R2_ACCESS_KEY_ID`` or ``AWS_ACCESS_KEY_ID``.
            secret_access_key: R2 secret. Falls back to
                ``R2_SECRET_ACCESS_KEY`` or ``AWS_SECRET_ACCESS_KEY``.
            endpoint_url: Optional explicit endpoint (testing). When
                not provided, the R2 account-specific endpoint is used.
            client_factory: Test seam passed through to the underlying
                :class:`S3ArtifactSink`.
        """
        resolved_bucket = bucket or os.environ.get("BERNSTEIN_R2_BUCKET") or ""
        resolved_account = account_id or os.environ.get("R2_ACCOUNT_ID")
        resolved_access = access_key_id or os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
        resolved_secret = (
            secret_access_key or os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
        if endpoint_url is None and resolved_account:
            endpoint_url = f"https://{resolved_account}.r2.cloudflarestorage.com"

        super().__init__(
            bucket=resolved_bucket,
            prefix=prefix,
            region="auto",
            endpoint_url=endpoint_url,
            access_key_id=resolved_access,
            secret_access_key=resolved_secret,
            session_token=None,
            client_factory=client_factory,
        )
        self._account_id = resolved_account


__all__ = ["R2ArtifactSink"]
