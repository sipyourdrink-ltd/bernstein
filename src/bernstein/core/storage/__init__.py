"""Pluggable artifact storage sinks (oai-003).

Bernstein's ``.sdd/`` layer is the durable substrate behind the WAL,
HMAC audit log, runtime state, task outputs, metrics dumps, and cost
ledgers. This package replaces the hard-coded local-filesystem
writes with a protocol-based abstraction so deployments can redirect
artifacts to S3, GCS, Azure Blob, or Cloudflare R2 without touching
business logic.

Public API::

    from bernstein.core.storage import (
        ArtifactSink,
        ArtifactSinkConformance,
        ArtifactStat,
        BufferedSink,
        BufferedSinkStats,
        LocalFsSink,
        SinkError,
        get_sink,
        list_sink_names,
        list_sinks,
        register_sink,
    )

Cloud sinks live in :mod:`bernstein.core.storage.sinks` behind lazy
imports: the module hierarchy is always importable, but instantiating
a cloud sink without the corresponding optional SDK raises a clear
``<Provider>Unavailable`` error pointing at the right extra.

See ``docs/architecture/storage.md`` for the end-to-end design and
trade-offs per provider.
"""

from __future__ import annotations

from bernstein.core.storage.buffered import BufferedSink, BufferedSinkStats
from bernstein.core.storage.conformance import ArtifactSinkConformance
from bernstein.core.storage.registry import (
    default_registry,
    get_sink,
    list_sink_names,
    list_sinks,
    register_sink,
)
from bernstein.core.storage.sink import (
    ArtifactSink,
    ArtifactStat,
    SinkError,
    join_keys,
    normalise_key,
)
from bernstein.core.storage.sinks.local_fs import LocalFsSink

__all__ = [
    "ArtifactSink",
    "ArtifactSinkConformance",
    "ArtifactStat",
    "BufferedSink",
    "BufferedSinkStats",
    "LocalFsSink",
    "SinkError",
    "default_registry",
    "get_sink",
    "join_keys",
    "list_sink_names",
    "list_sinks",
    "normalise_key",
    "register_sink",
]
