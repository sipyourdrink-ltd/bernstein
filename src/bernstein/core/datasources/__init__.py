"""Content-addressed query receipts (issue #2887).

Operator-configured, read-only SQL connections whose results are canonicalised
and bound into a signed lineage receipt: the exact result set that grounded an
agent's answer is a content-addressed, verifiable record rather than
unrecorded prompt text.

Public surface:

* :mod:`bernstein.core.datasources.result` -- engine-agnostic canonical
  encoding + ``content_hash``.
* :mod:`bernstein.core.datasources.engine` -- read-only statement guard and the
  stdlib-sqlite reference engine.
* :mod:`bernstein.core.datasources.connection` -- connection config + registry.
* :mod:`bernstein.core.datasources.receipt` -- record / verify / drift.
"""

from __future__ import annotations

__all__: list[str] = []
