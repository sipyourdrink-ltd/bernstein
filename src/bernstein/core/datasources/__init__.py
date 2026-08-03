"""Content-addressed query receipts (issue #2887) and the schema-bound query driver (issue #3125).

Operator-configured, read-only SQL connections whose results are canonicalised
and bound into a signed lineage receipt: the exact result set that grounded an
agent's answer is a content-addressed, verifiable record rather than
unrecorded prompt text. The query driver extends the same posture onto the
typed activity boundary: it executes one read-only statement through
``DataActivity``, records the canonical schema snapshot and the query text +
parameters as signed inputs before the plan, and records the canonicalised
result bytes (explicit row order) as a signed output -- so a number can be
traced back to the statement, the parameters, and the schema state it was
derived against, and schema drift is a typed refusal rather than a silently
different answer.

Public surface:

* :mod:`bernstein.core.datasources.result` -- engine-agnostic canonical
  encoding + ``content_hash``.
* :mod:`bernstein.core.datasources.engine` -- read-only statement guard and the
  stdlib-sqlite reference engine.
* :mod:`bernstein.core.datasources.connection` -- connection config + registry.
* :mod:`bernstein.core.datasources.receipt` -- record / verify / drift.
* :mod:`bernstein.core.datasources.schema` -- canonical schema snapshot,
  content digest, per-object drift diff.
* :mod:`bernstein.core.datasources.query_driver` -- the read-only query driver
  behind ``DataActivity`` (signed schema/query inputs, canonical result
  output, fail-closed :class:`~bernstein.core.datasources.errors.SchemaDrift`).
"""

from __future__ import annotations

__all__: list[str] = []
