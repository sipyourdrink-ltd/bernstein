"""First-party :class:`~bernstein.core.storage.sink.ArtifactSink` implementations.

The local-filesystem sink is always importable. Cloud sinks
(:mod:`~bernstein.core.storage.sinks.s3`,
:mod:`~bernstein.core.storage.sinks.gcs`,
:mod:`~bernstein.core.storage.sinks.azure_blob`,
:mod:`~bernstein.core.storage.sinks.r2`) import their provider SDKs
lazily inside their ``__init__`` / operation methods, so pulling this
package never forces the optional extras.
"""

from __future__ import annotations

from bernstein.core.storage.sinks.local_fs import LocalFsSink

__all__ = ["LocalFsSink"]
