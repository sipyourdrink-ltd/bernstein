"""Read-only query driver binding a result to the schema snapshot behind it.

:class:`~bernstein.core.orchestration.activity_modalities.DataActivity` ships a
complete phase machine -- signed inputs, one deterministic plan, signed
outputs, ``DataOpsPhaseError`` on any output before a plan -- but nothing in
the tree hands it a query to run. This module is that driver.

What it adds, honestly stated: databases already log every statement, and
re-running a statement is free, so statement logging is not the gap. The gap
is twofold. First, the schema state a statement was written against is nowhere
in the record -- a statement that was correct in March and means something
different in July looks identical in the log. Second, the bytes that actually
left the system are not bound to the statement that produced them. The driver
closes both: the canonical schema snapshot and the query text + parameters are
recorded as signed inputs *before* the plan is derived, and the canonicalised
result bytes are recorded as a signed output bound to that plan, all through
``DataActivity``'s existing phase machine and Ed25519 signing (this module is
a driver, not a second receipt implementation).

Guarantees:

* **Read-only at the boundary.** The submitted statement passes
  :func:`~bernstein.core.datasources.engine.guard_read_only` before any
  connection work, so a write is refused before the connection executes
  anything; the sqlite backend then executes behind ``mode=ro`` and a
  deny-all-writes authorizer as defense in depth.
* **Explicit row order.** An engine's row order without ``ORDER BY`` is an
  unspecified plan detail, so the driver canonicalises it: rows are sorted by
  their canonical cell bytes (:func:`canonicalize_row_order`) before encoding,
  making the result bytes a pure function of the logical result.
* **Fail-closed drift.** When the caller pins the schema snapshot a statement
  was recorded against and the live digest differs, the driver raises a typed
  :class:`~bernstein.core.datasources.errors.SchemaDrift` naming the changed
  objects instead of returning a number whose meaning changed. The same typed
  refusal fires when the schema changes *between* the signed snapshot and the
  execution: the driver re-snapshots after executing and refuses to sign a
  result the recorded snapshot may not describe.

The reference backend is the stdlib ``sqlite3`` driver (Python DB-API); the
:class:`QueryDriverBackend` protocol is the seam a DuckDB / PostgreSQL backend
slots in behind later without touching the receipt path.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, NoReturn, Protocol, runtime_checkable

from bernstein.core.datasources.engine import DEFAULT_ROW_CAP, SqliteEngine, guard_read_only
from bernstein.core.datasources.errors import DataSourceError, SchemaDrift, UnsupportedValue
from bernstein.core.datasources.result import NormalizedResult, canonical_bytes, content_hash
from bernstein.core.datasources.schema import SchemaSnapshot, diff_snapshots, snapshot_schema
from bernstein.core.orchestration.activity_modalities import (
    ContentStore,
    DataActivity,
    DataOpsPlan,
    DataOpsReceipt,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.datasources.connection import DataSourceConnection
    from bernstein.core.orchestration.activity import ActivityResult

#: Query-input format version. Version 2 replaced the JSON encoding (whose
#: ``default=str`` fallback collapsed e.g. ``Decimal("1.5")`` and ``"1.5"``
#: onto identical bytes) with the type-tagged canonical encoding below.
QUERY_INPUT_VERSION = 2

#: The declared plan steps. The plan hash covers these plus the signed input
#: hashes, so the receipt attests both *what* ran and *which* canonicalisation
#: produced the output bytes.
PLAN_STEPS: tuple[str, ...] = ("execute-read-only-query", "canonicalise-row-order")


@runtime_checkable
class QueryDriverBackend(Protocol):
    """The engine seam behind :class:`ReadOnlyQueryDriver`.

    A backend supplies exactly two things: a canonical snapshot of the live
    schema and guarded read-only execution onto
    :class:`~bernstein.core.datasources.result.NormalizedResult`. DuckDB /
    PostgreSQL backends implement this same protocol later; the driver's
    receipt path does not change.
    """

    @property
    def name(self) -> str:
        """Engine name recorded as provenance (e.g. ``sqlite``)."""
        ...

    def schema_snapshot(self) -> SchemaSnapshot:
        """Return the canonical snapshot of the live schema."""
        ...

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> NormalizedResult:
        """Run ``sql`` read-only and return the normalised result."""
        ...


class SqliteQueryBackend:
    """Reference backend: stdlib ``sqlite3`` (Python DB-API), no new dependency.

    File databases are opened read-only (``file:...?mode=ro``); execution goes
    through :class:`~bernstein.core.datasources.engine.SqliteEngine`, which
    installs the deny-all-writes authorizer. Tests may inject an open
    connection (e.g. ``:memory:`` or a traced connection) instead of a path.
    """

    name = "sqlite"

    def __init__(self, db_path: str | None = None, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None and db_path is None:
            raise ValueError("SqliteQueryBackend requires either db_path or connection")
        self._db_path = db_path
        self._conn = connection

    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(connection, owned)``; owned connections are closed by us."""
        if self._conn is not None:
            return self._conn, False
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        except sqlite3.DatabaseError as exc:
            raise DataSourceError(f"cannot open database read-only: {exc}") from exc
        return conn, True

    def schema_snapshot(self) -> SchemaSnapshot:
        conn, owned = self._connect()
        try:
            return snapshot_schema(conn)
        finally:
            if owned:
                conn.close()

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> NormalizedResult:
        engine = SqliteEngine(self._db_path, connection=self._conn)
        return engine.execute(sql, params, row_cap=row_cap)


def _raise_schema_drift(recorded: SchemaSnapshot, live: SchemaSnapshot, *, message: str) -> NoReturn:
    """Build and raise the typed drift refusal from one recorded/live pair.

    Both refusal sites (pre-execution pin check, post-execution re-check)
    share this flow so the drift payload and message shape cannot diverge.
    """
    drifts = diff_snapshots(recorded, live)
    named = "; ".join(d.describe() for d in drifts) or "digests differ but no object-level diff resolved"
    raise SchemaDrift(
        f"{message}: {named}",
        recorded_digest=recorded.digest,
        live_digest=live.digest,
        drifts=drifts,
    )


def _row_key(result: NormalizedResult, row: tuple[object, ...]) -> bytes:
    """Canonical sort key for one row: its single-row canonical encoding.

    Every single-row projection of the same result shares an identical header
    (same columns, fixed flags), so ordering by the full encoding is ordering
    by the row's canonical cell bytes -- a total order that cannot depend on
    which plan the engine picked.
    """
    return canonical_bytes(NormalizedResult(columns=result.columns, rows=(row,), truncated=False, row_cap=0))


def canonicalize_row_order(result: NormalizedResult) -> NormalizedResult:
    """Return *result* with rows in explicit canonical order.

    A SQL engine's row order without ``ORDER BY`` is an unspecified plan
    detail: it can change with an index, a table rewrite, or a version bump.
    The driver therefore never leaves row order to the engine -- rows are
    sorted by their canonical cell bytes, so two runs that produced the same
    logical row set encode to byte-identical canonical output. (For a
    truncated result the surviving row *subset* still depends on engine order;
    the truncation flag is inside the hashed body precisely so a truncated
    result can never pose as a total one.)
    """
    ordered = tuple(sorted(result.rows, key=lambda row: _row_key(result, row)))
    return NormalizedResult(
        columns=result.columns,
        rows=ordered,
        truncated=result.truncated,
        row_cap=result.row_cap,
    )


# --- signed query input: type-tagged canonical encoding ----------------------

#: Format magic for the signed query-input blob (bernstein query input, v2).
_QUERY_INPUT_MAGIC = b"bqi\x02"

# One-byte type tags for bound parameters, aligned with the result encoding's
# cell tags where the types overlap so the two surfaces read consistently.
_P_NULL = b"n"
_P_BOOLEAN = b"o"
_P_INTEGER = b"i"
_P_FLOAT = b"f"
_P_DECIMAL = b"d"
_P_TEXT = b"t"
_P_BLOB = b"b"
_P_DATETIME = b"z"
_P_DATE = b"y"


def _field(payload: bytes) -> bytes:
    """Length-prefix ``payload`` netstring-style: ``<len>:<payload>``."""
    return str(len(payload)).encode("ascii") + b":" + payload


def _param_scalar_bytes(value: object) -> bytes:
    """Render one bound parameter to tagged canonical bytes, type-preserving.

    Every supported DB-API parameter type carries its own one-byte tag, so two
    parameters that differ only in type (``1`` vs ``"1"`` vs ``1.0`` vs
    ``Decimal("1")`` vs ``b"1"`` vs ``True``) can never render to the same
    bytes. An unsupported type is refused with a typed error -- never silently
    stringified, because a lossy fallback is exactly what lets two distinct
    queries share one signed input hash.
    """
    if value is None:
        return _P_NULL
    # ``bool`` before ``int`` (subclass); ``datetime`` before ``date`` (subclass).
    if isinstance(value, bool):
        return _P_BOOLEAN + (b"1" if value else b"0")
    if isinstance(value, int):
        return _P_INTEGER + str(value).encode("ascii")
    if isinstance(value, float):
        return _P_FLOAT + repr(value).encode("ascii")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise UnsupportedValue(f"non-finite Decimal bound parameter: {value!r}")
        return _P_DECIMAL + format(value, "f").encode("ascii")
    if isinstance(value, str):
        return _P_TEXT + value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return _P_BLOB + bytes(value)
    if isinstance(value, _dt.datetime):
        return _P_DATETIME + value.isoformat().encode("utf-8")
    if isinstance(value, _dt.date):
        return _P_DATE + value.isoformat().encode("utf-8")
    raise UnsupportedValue(f"cannot canonically encode bound parameter of type {type(value).__name__}: {value!r}")


def query_input_bytes(sql: str, params: Sequence[object] | Mapping[str, object] | None) -> bytes:
    """Canonical bytes of the signed query input (statement + bound parameters).

    The encoding is type-preserving and deterministic: the statement text and
    every parameter are length-prefixed (netstring-style) and each parameter
    carries a one-byte type tag, so the same statement with the same
    parameters signs to the same input hash on every run, and parameters that
    differ in type or value always sign differently. Positional and named
    parameter shapes are framed distinctly (``seq`` vs ``map``), and mapping
    keys are encoded in sorted order so dict iteration order cannot leak into
    the hash. Unsupported parameter types are refused with a typed error.
    """
    parts: list[bytes] = [_QUERY_INPUT_MAGIC]
    parts.append(_field(b"v=" + str(QUERY_INPUT_VERSION).encode("ascii")))
    parts.append(_field(sql.encode("utf-8")))
    if params is None:
        parts.append(_field(b"params=none"))
    elif isinstance(params, Mapping):
        parts.append(_field(b"params=map"))
        parts.append(_field(str(len(params)).encode("ascii")))
        # Keys are str by the DB-API named-parameter contract (and by this
        # function's signature); sorted order keeps dict iteration order out
        # of the signed bytes.
        for key in sorted(params):
            parts.append(_field(key.encode("utf-8")))
            parts.append(_field(_param_scalar_bytes(params[key])))
    else:
        parts.append(_field(b"params=seq"))
        parts.append(_field(str(len(params)).encode("ascii")))
        for value in params:
            parts.append(_field(_param_scalar_bytes(value)))
    return b"".join(parts)


@dataclass(frozen=True, slots=True)
class QueryRun:
    """The outcome of one driver execution: result, schema binding, receipt.

    Attributes:
        result: The normalised result in canonical row order.
        canonical_result_bytes: The exact bytes recorded as the signed output.
        result_hash: ``sha256:`` over ``canonical_result_bytes``.
        schema: The schema snapshot the result was derived against.
        schema_digest: The snapshot's content digest (a signed input hash).
        plan: The deterministic plan derived from the signed inputs.
        receipt: The signed :class:`DataOpsReceipt` (the primary artifact).
        activity_result: The typed activity result; its ``artifact_hash`` is
            the receipt hash the journal anchors.
    """

    result: NormalizedResult
    canonical_result_bytes: bytes
    result_hash: str
    schema: SchemaSnapshot
    schema_digest: str
    plan: DataOpsPlan
    receipt: DataOpsReceipt
    activity_result: ActivityResult

    @property
    def receipt_hash(self) -> str:
        """The ``sha256:`` hash of the receipt's canonical projection."""
        return self.activity_result.artifact_hash


class ReadOnlyQueryDriver:
    """Execute one read-only query through ``DataActivity``, schema-bound.

    Phase choreography per :meth:`run` (one activity per execution):

    1. The submitted statement passes the textual read-only guard *before any
       connection work* -- a write is refused with a typed error and nothing
       ever reaches the engine.
    2. The live schema snapshot is taken and, when the caller pinned the
       snapshot a statement was recorded against, compared fail-closed: a
       digest mismatch raises :class:`SchemaDrift` naming the changed objects
       and the query never executes.
    3. Snapshot and query + parameters are recorded as signed inputs; the
       deterministic plan is derived from them (inputs are frozen from here).
    4. The statement executes on the backend (read-only, authorizer-guarded).
    5. The schema is snapshotted *again* and compared to the signed snapshot:
       a digest mismatch means the schema moved between snapshot and
       execution, so the driver raises :class:`SchemaDrift` and refuses to
       sign a result the recorded snapshot may not describe.
    6. The rows are put into explicit canonical order and the canonical
       result bytes are recorded as the signed output bound to the plan.

    The resulting :class:`DataOpsReceipt` verifies offline through the
    existing :func:`~bernstein.core.orchestration.activity_modalities.verify_data_ops_receipt`.
    """

    def __init__(
        self,
        backend: QueryDriverBackend,
        *,
        store: ContentStore,
        private_key_pem: str,
        public_key_pem: str,
        connection_id: str = "",
    ) -> None:
        self._backend = backend
        self._store = store
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem
        # Provenance label only: refs carry the connection *id*, never a DSN,
        # so credentials cannot enter the recorded artifact by construction.
        self._connection_id = connection_id or backend.name

    def new_activity(self) -> DataActivity:
        """Return the :class:`DataActivity` the driver drives, unstarted.

        Exposed so callers (and tests) can exercise the phase contract
        directly against the driver's own store and keys: recording an output
        before a plan raises
        :class:`~bernstein.core.orchestration.activity_modalities.DataOpsPhaseError`.
        """
        return DataActivity(
            store=self._store,
            private_key_pem=self._private_key_pem,
            public_key_pem=self._public_key_pem,
        )

    def run(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
        expected_schema: SchemaSnapshot | None = None,
    ) -> QueryRun:
        """Execute *sql* read-only and bind the result to the live schema.

        Args:
            sql: A single read statement. Anything else is refused before the
                connection executes anything.
            params: Optional bound parameters (positional or named).
            row_cap: Per-run row cap; truncation is folded into the hash.
            expected_schema: When given, the snapshot the statement was
                recorded against; a live digest mismatch fails closed.

        Returns:
            The :class:`QueryRun` with the signed receipt.

        Raises:
            UnsupportedStatement: Empty or multi-statement input.
            ReadOnlyViolation: A write statement, refused before execution.
            SchemaDrift: The live schema diverged from ``expected_schema``, or
                the schema changed between the signed snapshot and execution
                (the driver refuses to sign the result in both cases).
            UnsupportedValue: A bound parameter of an unencodable type.
        """
        # Phase 1: refuse a write before any connection work. The guard is
        # textual and runs first, so a refused statement provably never
        # reached the engine (the backend's authorizer remains as defense in
        # depth behind it).
        statement = guard_read_only(sql)

        # Phase 2: bind to the live schema, fail closed on drift.
        schema = self._backend.schema_snapshot()
        if expected_schema is not None and expected_schema.digest != schema.digest:
            _raise_schema_drift(expected_schema, schema, message=f"schema drift on {self._connection_id!r}")

        # Phase 3: signed inputs, then the deterministic plan (inputs freeze).
        activity = self.new_activity()
        activity.add_input(ref=f"schema:{self._connection_id}", content=schema.canonical_bytes())
        activity.add_input(ref=f"query:{self._connection_id}", content=query_input_bytes(statement, params))
        plan = activity.plan(PLAN_STEPS)

        # Phase 4: guarded execution.
        engine_result = self._backend.execute(statement, params, row_cap=row_cap)

        # Phase 5: re-snapshot before signing, fail closed. The snapshot and
        # the execution may run on separate connections, so a schema change in
        # the gap could otherwise leave the signed snapshot attesting a schema
        # the result was not derived against. SQLite blocks a schema change
        # for the duration of a single executing statement; this check closes
        # the between-statements window. (A change that is reverted between
        # the two snapshots -- same digest on both sides -- is not detectable
        # from snapshots alone.)
        post_schema = self._backend.schema_snapshot()
        if post_schema.digest != schema.digest:
            _raise_schema_drift(
                schema,
                post_schema,
                message=f"schema changed during execution on {self._connection_id!r}; refusing to sign the result",
            )

        # Phase 6: explicit row order, signed output.
        result = canonicalize_row_order(engine_result)
        result_bytes = canonical_bytes(result)
        activity.add_output(ref=f"result:{self._connection_id}", content=result_bytes)

        activity_result = activity.finish()
        return QueryRun(
            result=result,
            canonical_result_bytes=result_bytes,
            result_hash=content_hash(result),
            schema=schema,
            schema_digest=schema.digest,
            plan=plan,
            receipt=DataOpsReceipt.from_dict(activity_result.artifact),
            activity_result=activity_result,
        )


def build_sqlite_query_driver(sdd_dir: Path, connection: DataSourceConnection) -> ReadOnlyQueryDriver:
    """Return a driver for a registered sqlite connection, keys provisioned.

    Signing identity and content store live under ``<sdd>/datasources`` with
    the same key files the query-receipt store uses, so the whole datasource
    subsystem signs under one install identity. The driver records only
    ``connection.id`` as provenance -- never the DSN.
    """
    from bernstein.core.datasources.service import PRIVATE_KEY_NAME, PUBLIC_KEY_NAME, datasources_root
    from bernstein.core.lineage.identity import load_or_create_signing_identity

    if connection.driver != "sqlite":
        raise DataSourceError(f"unsupported driver {connection.driver!r} (only 'sqlite' ships today)")
    root = datasources_root(sdd_dir)
    private_pem, public_pem = load_or_create_signing_identity(
        root / "identity",
        private_name=PRIVATE_KEY_NAME,
        public_name=PUBLIC_KEY_NAME,
    )
    return ReadOnlyQueryDriver(
        SqliteQueryBackend(connection.dsn),
        store=ContentStore(root / "cas"),
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        connection_id=connection.id,
    )


__all__ = [
    "PLAN_STEPS",
    "QUERY_INPUT_VERSION",
    "QueryDriverBackend",
    "QueryRun",
    "ReadOnlyQueryDriver",
    "SqliteQueryBackend",
    "build_sqlite_query_driver",
    "canonicalize_row_order",
    "query_input_bytes",
]
