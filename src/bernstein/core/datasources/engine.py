"""Query engines: the read-only statement guard + reference adapters.

An *engine* takes a SQL string plus bound parameters and returns a
:class:`~bernstein.core.datasources.result.NormalizedResult` -- the
engine-agnostic shape the canonical encoder digests. Two engines that produce
the same logical result therefore produce the same ``content_hash``.

Reference engines shipped here:

* :class:`SqliteEngine` -- the stdlib-``sqlite3`` reference engine. Opens the
  database read-only (``mode=ro``), installs a SQL authorizer that denies every
  write action, and runs the query behind the textual read-only guard. No third
  party driver, no network.

* :class:`InMemoryEngine` -- a hand-specified reference producer: it builds a
  ``NormalizedResult`` from pre-shaped column/row data with no SQL engine at
  all. It proves the sqlite adapter matches an independent *spec* of the
  canonical result, but because it is handed the already-canonical values it
  does not, on its own, demonstrate cross-engine *type reconciliation*.

* :class:`ColumnStoreEngine` -- a second, genuinely independent engine used to
  demonstrate that reconciliation empirically. It is fed the *raw* source data
  (booleans as ``0``/``1`` ints, decimals as strings, blobs as raw bytes),
  carries its own Arrow-style logical type system (``int64`` / ``utf8`` /
  ``float64`` / ``bool`` / ``binary`` / ``decimal128``), coerces cells through
  its own code path, and runs the *same SQL string* through its own projection +
  ORDER BY executor. When :class:`SqliteEngine` and :class:`ColumnStoreEngine`
  are loaded from the same fixture and run the same query, they emit a
  byte-identical ``content_hash`` -- agreement across two backends with disjoint
  type-resolution code, not a shared spec.

A third-party Arrow-interchange warehouse adapter (DuckDB, Postgres, ...) is a
noted follow-up (those drivers are absent from the pinned environment): it slots
in behind the same :class:`QueryEngine` protocol by normalising its result onto
:class:`NormalizedResult`; the canonical encoding and receipt layers do not
change.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bernstein.core.datasources import result as _result
from bernstein.core.datasources.errors import ReadOnlyViolation, UnsupportedStatement, UnsupportedValue
from bernstein.core.datasources.result import NormalizedColumn, NormalizedResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Default per-receipt row cap. A result longer than this is truncated and the
#: truncation flag is folded into the hashed body.
DEFAULT_ROW_CAP = 10_000

# Statement-leading keywords that begin a read query.
_READ_LEADERS = frozenset({"select", "with", "values", "explain", "pragma"})

# Keywords that, if they appear as a bare token anywhere in the statement,
# mark it as a write. Guards ``WITH ... INSERT`` and friends that a
# leading-keyword check alone would miss.
_WRITE_TOKENS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "replace",
        "merge",
        "upsert",
        "create",
        "drop",
        "alter",
        "truncate",
        "grant",
        "revoke",
        "attach",
        "detach",
        "vacuum",
        "reindex",
        "commit",
        "rollback",
        "begin",
        "savepoint",
    }
)


def _strip_sql(sql: str) -> str:
    """Remove line/block comments and string/identifier literals from ``sql``.

    The result keeps SQL keywords and whitespace but blanks out anything a
    literal could hide, so the keyword scan below cannot be fooled by a write
    keyword buried inside a quoted string.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    # Doubled quote is an escaped quote inside the literal.
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _tokens(stripped: str) -> list[str]:
    """Lowercase word tokens of a comment/literal-stripped statement."""
    tok: list[str] = []
    cur: list[str] = []
    for ch in stripped:
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        elif cur:
            tok.append("".join(cur).lower())
            cur = []
    if cur:
        tok.append("".join(cur).lower())
    return tok


def guard_read_only(sql: str) -> str:
    """Return the single trimmed statement, or raise a typed error.

    Enforces, textually and before any execution:

    * exactly one statement (trailing ``;`` allowed, embedded ``;`` refused),
    * a read leader (``SELECT`` / ``WITH`` / ``VALUES`` / ``EXPLAIN`` /
      ``PRAGMA``),
    * no write keyword anywhere in the statement body.

    Raises:
        UnsupportedStatement: empty input or more than one statement.
        ReadOnlyViolation: a DML/DDL leader or a write keyword in the body.
    """
    if not sql or not sql.strip():
        raise UnsupportedStatement("empty SQL statement")

    stripped = _strip_sql(sql)
    # Split on ';' over the *stripped* text so a ';' inside a literal is ignored.
    statements = [s for s in stripped.split(";") if s.strip()]
    if len(statements) > 1:
        raise UnsupportedStatement("only a single statement is allowed per query")
    if not statements:
        raise UnsupportedStatement("no executable statement found")

    toks = _tokens(statements[0])
    if not toks:
        raise UnsupportedStatement("no executable statement found")
    if toks[0] not in _READ_LEADERS:
        raise ReadOnlyViolation(f"statement is not a read query (leading keyword {toks[0].upper()!r})")
    for t in toks:
        if t in _WRITE_TOKENS:
            raise ReadOnlyViolation(f"write keyword {t.upper()!r} is not allowed in a read query")
    return sql.strip()


@runtime_checkable
class QueryEngine(Protocol):
    """A read-only query engine that yields a normalised result."""

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> NormalizedResult:
        """Run ``sql`` read-only and return a :class:`NormalizedResult`."""
        ...


# --- sqlite reference engine ------------------------------------------------

# Map sqlite declared column-type affinity onto a canonical column type. The
# lookup is by substring, per SQLite's own affinity rules (§3.1 "Determination
# Of Column Affinity"), so ``VARCHAR(20)`` -> text, ``BIGINT`` -> integer.
#
# Note on decimals: SQLite has no decimal type. A ``DECIMAL`` / ``NUMERIC``
# column carries NUMERIC affinity, which converts a value like ``"1.50"`` to
# the real ``1.5`` on insert -- the fixed scale is gone before we ever read it.
# The reference engine therefore maps numeric affinity to canonical ``float``
# and never fabricates a scale it cannot prove. Faithful fixed-scale decimals
# are a property of the canonical encoding (verified directly, and surfaced by
# any decimal-carrying engine such as the InMemory reference or a future
# Arrow-interchange warehouse adapter), not something SQLite can attest.
_SQLITE_AFFINITY: tuple[tuple[str, str], ...] = (
    ("int", _result.INTEGER),
    ("char", _result.TEXT),
    ("clob", _result.TEXT),
    ("text", _result.TEXT),
    ("blob", _result.BLOB),
    ("real", _result.FLOAT),
    ("floa", _result.FLOAT),
    ("doub", _result.FLOAT),
    ("bool", _result.BOOLEAN),
    ("dec", _result.FLOAT),
    ("num", _result.FLOAT),
)


def _sqlite_column_type(declared: str | None) -> str:
    """Canonical column type for a sqlite declared type string.

    Unknown / expression columns (no declared type) fall back to ``text`` --
    a deterministic default that keeps cross-run identity while leaving the
    per-cell rendering to decide the actual value shape.
    """
    if not declared:
        return _result.TEXT
    d = declared.lower()
    for needle, canon in _SQLITE_AFFINITY:
        if needle in d:
            return canon
    return _result.TEXT


def _normalize_sqlite_cell(col_type: str, value: object) -> object:
    """Coerce a raw sqlite cell into a canonical Python value.

    sqlite is dynamically typed, so the column's declared canonical type drives
    coercion into a *homogeneous* column: a ``boolean`` column materialises its
    cells as ``bool``; a ``float`` column widens integer-affinity cells (e.g.
    ``0`` from ``"0.00"``) to ``float`` so a numeric column never mixes ``int``
    and ``float`` across rows. Types that already match pass through untouched.
    """
    if value is None:
        return None
    if col_type == _result.BOOLEAN and isinstance(value, int):
        return bool(value)
    if col_type == _result.FLOAT and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str, bytes, bool, Decimal)):
        return value
    raise UnsupportedValue(f"sqlite returned an unsupported cell type {type(value).__name__}")


class SqliteEngine:
    """Read-only sqlite3 reference engine.

    Opens ``db_path`` read-only via a ``file:...?mode=ro`` URI and installs an
    authorizer that denies every write action, so even a guard bypass cannot
    mutate the database. ``:memory:`` databases (tests) are opened directly on a
    caller-supplied connection instead.
    """

    def __init__(self, db_path: str | None = None, *, connection: sqlite3.Connection | None = None) -> None:
        if connection is None and db_path is None:
            raise ValueError("SqliteEngine requires either db_path or connection")
        self._db_path = db_path
        self._conn = connection

    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        """Return ``(connection, owned)``; ``owned`` connections are closed by us."""
        if self._conn is not None:
            return self._conn, False
        if self._db_path is None:  # pragma: no cover - guarded in __init__
            raise ValueError("SqliteEngine has neither a db_path nor a connection")
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        return conn, True

    @staticmethod
    def _deny_writes(
        action: int,
        _arg1: str | None,
        _arg2: str | None,
        _dbname: str | None,
        _trigger: str | None,
    ) -> int:
        # Allow only read/metadata actions; deny anything that writes.
        write_actions = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
        }
        return sqlite3.SQLITE_DENY if action in write_actions else sqlite3.SQLITE_OK

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> NormalizedResult:
        statement = guard_read_only(sql)
        conn, owned = self._connect()
        try:
            conn.set_authorizer(self._deny_writes)
            try:
                cur = conn.execute(statement, _as_bind(params))
            except sqlite3.DatabaseError as exc:
                # A write that slipped the textual guard is still stopped at the
                # engine: the authorizer denial surfaces as ``not authorized`` and
                # the ``mode=ro`` connection rejects a write (e.g. a value-setting
                # ``PRAGMA``) with ``attempt to write a readonly database``. Both
                # re-raise as our typed read-only error rather than a raw sqlite
                # error, so the CLI's ``DataSourceError`` handler reports them as a
                # named refusal instead of an unhandled crash.
                message = str(exc).lower()
                if "not authorized" in message or "readonly" in message or "read-only" in message:
                    raise ReadOnlyViolation(f"statement denied by read-only enforcement: {exc}") from exc
                raise
            columns = _sqlite_columns(conn, cur)
            rows: list[tuple[object, ...]] = []
            truncated = False
            for raw in cur:
                if len(rows) >= row_cap:
                    truncated = True
                    break
                rows.append(
                    tuple(_normalize_sqlite_cell(col.type, cell) for col, cell in zip(columns, raw, strict=True))
                )
        finally:
            conn.set_authorizer(None)
            if owned:
                conn.close()
        return NormalizedResult(
            columns=tuple(columns),
            rows=tuple(rows),
            truncated=truncated,
            row_cap=row_cap,
        )


def _as_bind(params: Sequence[object] | Mapping[str, object] | None) -> Sequence[Any] | Mapping[str, Any]:
    """Normalise bound parameters into a form ``sqlite3.execute`` accepts."""
    if params is None:
        return ()
    return params


def _sqlite_columns(conn: sqlite3.Connection, cur: sqlite3.Cursor) -> list[NormalizedColumn]:
    """Derive canonical columns from a cursor's description + table schema.

    ``cursor.description`` gives names but not types for expression columns, so
    the declared type is looked up from the base table via ``PRAGMA``-style
    schema when the column maps to a plain table column. Expression columns
    fall back to ``text``.
    """
    description = cur.description or ()
    names = [d[0] for d in description]
    # Best-effort declared-type lookup: for a single-table SELECT the base
    # column type is recoverable; otherwise default to text. We keep this
    # deterministic and cheap rather than parsing SQL.
    declared_by_name = _declared_types(conn)
    columns: list[NormalizedColumn] = []
    for name in names:
        canon = _sqlite_column_type(declared_by_name.get(name))
        columns.append(NormalizedColumn(name=name, type=canon))
    return columns


def _declared_types(conn: sqlite3.Connection) -> dict[str, str]:
    """Map ``column name -> declared type`` across all user tables.

    A name collision across tables is resolved by first-seen; the canonical
    encoding still records whatever type the engine reports, and cross-engine
    identity is a property of the fixture (callers keep names unambiguous).
    """
    out: dict[str, str] = {}
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.DatabaseError:
        return out
    for (table,) in tables:
        try:
            for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})"):
                col_name, col_type = row[1], row[2]
                if col_name not in out and col_type:
                    out[col_name] = col_type
        except sqlite3.DatabaseError:
            continue
    return out


def _quote_ident(ident: str) -> str:
    """Quote a sqlite identifier for interpolation into a PRAGMA."""
    return '"' + ident.replace('"', '""') + '"'


# --- in-memory reference engine ---------------------------------------------


class InMemoryEngine:
    """Independent reference producer with no SQL engine.

    Built from explicit column definitions and rows. It exists so tests can
    prove the canonical encoding is engine-agnostic: given a type-equivalent
    fixture, its ``content_hash`` equals :class:`SqliteEngine`'s. It ignores the
    ``sql`` argument (there is no engine to run it) and only applies the row cap.
    """

    def __init__(self, columns: Sequence[tuple[str, str]], rows: Sequence[Sequence[object]]) -> None:
        self._columns = tuple(NormalizedColumn(name=n, type=t) for n, t in columns)
        self._rows = tuple(tuple(r) for r in rows)

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> NormalizedResult:
        # No engine to run: the fixture supplied at construction *is* the result;
        # ``sql`` and ``params`` are accepted only to satisfy the protocol.
        del sql, params
        rows = self._rows[:row_cap]
        truncated = len(self._rows) > row_cap
        return NormalizedResult(columns=self._columns, rows=rows, truncated=truncated, row_cap=row_cap)


# --- columnar reference engine ----------------------------------------------

# Arrow-style logical column types, resolved onto canonical types by *exact*
# base name. This vocabulary is deliberately disjoint from sqlite's affinity
# substrings (:data:`_SQLITE_AFFINITY`): the two engines share no
# type-resolution code, so a matching ``content_hash`` is genuine cross-engine
# agreement rather than a shared code path.
_LOGICAL_TYPES: dict[str, str] = {
    "int8": _result.INTEGER,
    "int16": _result.INTEGER,
    "int32": _result.INTEGER,
    "int64": _result.INTEGER,
    "uint32": _result.INTEGER,
    "uint64": _result.INTEGER,
    "float32": _result.FLOAT,
    "float64": _result.FLOAT,
    "double": _result.FLOAT,
    "bool": _result.BOOLEAN,
    "boolean": _result.BOOLEAN,
    "utf8": _result.TEXT,
    "large_utf8": _result.TEXT,
    "string": _result.TEXT,
    "binary": _result.BLOB,
    "large_binary": _result.BLOB,
    "decimal": _result.DECIMAL,
    "decimal128": _result.DECIMAL,
    "decimal256": _result.DECIMAL,
}

# The subset of SQL the reference executor understands. A query outside this
# shape (JOIN, GROUP BY, WHERE, expressions, ...) is refused as unsupported --
# the engine is a determinism reference, not a general SQL implementation.
_COLUMNSTORE_QUERY_RE = re.compile(
    r"^select\s+(?P<cols>.+?)\s+from\s+(?P<table>[a-z_][a-z0-9_]*)"
    r"(?:\s+order\s+by\s+(?P<order>[a-z_][a-z0-9_]*)(?:\s+(?P<dir>asc|desc))?)?"
    r"(?:\s+limit\s+(?P<limit>\d+))?$",
    re.IGNORECASE | re.DOTALL,
)
_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


@dataclass(frozen=True, slots=True)
class ColumnStoreTable:
    """One named table for :class:`ColumnStoreEngine`: typed columns + raw rows.

    ``columns`` is ``(name, logical_type)`` in physical order; ``rows`` holds the
    *raw* source values (ints for booleans, strings for decimals, raw ``bytes``
    for binary) exactly as an external columnar store would hand them over. The
    engine coerces them onto canonical values itself, so the fixture is source
    data -- not a pre-canonicalised result.
    """

    columns: Sequence[tuple[str, str]]
    rows: Sequence[Sequence[object]]


@dataclass(frozen=True, slots=True)
class _ColumnStorePlan:
    projection: tuple[str, ...] | None  # None -> SELECT *
    table: str
    order_by: str | None
    descending: bool
    limit: int | None


def _parse_columnstore_query(statement: str) -> _ColumnStorePlan:
    text = statement.rstrip().rstrip(";").strip()
    match = _COLUMNSTORE_QUERY_RE.match(text)
    if match is None:
        raise UnsupportedStatement(
            "ColumnStoreEngine supports only 'SELECT <cols> FROM <table> [ORDER BY <col> [ASC|DESC]] [LIMIT <n>]'"
        )
    cols_src = match.group("cols").strip()
    projection: tuple[str, ...] | None
    if cols_src == "*":
        projection = None
    else:
        parts = [c.strip() for c in cols_src.split(",")]
        if not parts or not all(_IDENT_RE.fullmatch(c) for c in parts):
            raise UnsupportedStatement("ColumnStoreEngine select list must be plain column names or '*'")
        projection = tuple(parts)
    limit_src = match.group("limit")
    return _ColumnStorePlan(
        projection=projection,
        table=match.group("table"),
        order_by=match.group("order"),
        descending=(match.group("dir") or "").lower() == "desc",
        limit=int(limit_src) if limit_src is not None else None,
    )


def _logical_canonical_type(logical: str) -> str:
    """Canonical type for an Arrow-style logical type name (own resolution)."""
    base = logical.strip().lower().split("(", 1)[0]
    canon = _LOGICAL_TYPES.get(base)
    if canon is None:
        raise UnsupportedValue(f"unknown logical column type: {logical!r}")
    return canon


def _order_key(value: object) -> tuple[bool, object]:
    """Sort key placing NULLs first on ascending order, matching sqlite."""
    return (value is not None, value)


def _coerce_columnstore_cell(canon_type: str, value: object) -> object:
    """Coerce one raw source cell onto a canonical value (own coercion path).

    Deliberately independent of :func:`_normalize_sqlite_cell`: a boolean column
    materialises ``0``/``1`` ints as ``bool``; a decimal column parses a raw
    string into a scale-preserving :class:`~decimal.Decimal`; a float column
    widens ints. Types that already match pass through.
    """
    if value is None:
        return None
    if canon_type == _result.BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        raise UnsupportedValue(f"boolean column cannot accept {type(value).__name__}")
    if canon_type == _result.INTEGER:
        if isinstance(value, bool):
            raise UnsupportedValue("integer column cannot accept a bool")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        raise UnsupportedValue(f"integer column cannot accept {type(value).__name__}")
    if canon_type == _result.FLOAT:
        if isinstance(value, bool):
            raise UnsupportedValue("float column cannot accept a bool")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value)
        raise UnsupportedValue(f"float column cannot accept {type(value).__name__}")
    if canon_type == _result.DECIMAL:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str):
            try:
                return Decimal(value)
            except InvalidOperation as exc:
                raise UnsupportedValue(f"decimal column cannot parse {value!r}") from exc
        if isinstance(value, int) and not isinstance(value, bool):
            return Decimal(value)
        raise UnsupportedValue(f"decimal column cannot accept {type(value).__name__}")
    if canon_type == _result.TEXT:
        if isinstance(value, str):
            return value
        raise UnsupportedValue(f"text column cannot accept {type(value).__name__}")
    if canon_type == _result.BLOB:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        raise UnsupportedValue(f"blob column cannot accept {type(value).__name__}")
    raise UnsupportedValue(f"unhandled canonical type {canon_type!r}")


class ColumnStoreEngine:
    """Independent columnar reference engine -- no sqlite, no third-party driver.

    Built from named :class:`ColumnStoreTable` fixtures of *raw* source values.
    It exists to demonstrate cross-engine determinism empirically: given the same
    fixture and the same SQL, its ``content_hash`` equals :class:`SqliteEngine`'s
    even though the two share no type-resolution or cell-coercion code. Only a
    documented SELECT subset is supported; anything else is refused as
    unsupported. Every query still passes :func:`guard_read_only`, so the engine
    refuses writes exactly like the sqlite reference.
    """

    def __init__(self, tables: Mapping[str, ColumnStoreTable]) -> None:
        self._tables = dict(tables)

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> NormalizedResult:
        statement = guard_read_only(sql)
        if params:
            # No bind-placeholder support in this reference engine; a query that
            # needs parameters is outside its documented subset.
            raise UnsupportedStatement("ColumnStoreEngine does not bind parameters")
        plan = _parse_columnstore_query(statement)
        table = self._tables.get(plan.table)
        if table is None:
            raise UnsupportedStatement(f"unknown table {plan.table!r}")
        return self._run(table, plan, row_cap)

    def _run(self, table: ColumnStoreTable, plan: _ColumnStorePlan, row_cap: int) -> NormalizedResult:
        phys = [(str(name), str(logical)) for name, logical in table.columns]
        index_of = {name: i for i, (name, _logical) in enumerate(phys)}
        if len(index_of) != len(phys):
            raise UnsupportedValue("ColumnStoreTable has duplicate column names")

        selected = [name for name, _logical in phys] if plan.projection is None else list(plan.projection)
        for name in selected:
            if name not in index_of:
                raise UnsupportedStatement(f"unknown column {name!r}")

        rows = [tuple(row) for row in table.rows]
        if plan.order_by is not None:
            if plan.order_by not in index_of:
                raise UnsupportedStatement(f"unknown ORDER BY column {plan.order_by!r}")
            order_index = index_of[plan.order_by]
            rows.sort(key=lambda row: _order_key(row[order_index]), reverse=plan.descending)
        if plan.limit is not None:
            rows = rows[: plan.limit]

        truncated = len(rows) > row_cap
        rows = rows[:row_cap]

        out_columns = tuple(
            NormalizedColumn(name=name, type=_logical_canonical_type(phys[index_of[name]][1])) for name in selected
        )
        out_rows = tuple(
            tuple(_coerce_columnstore_cell(col.type, row[index_of[col.name]]) for col in out_columns) for row in rows
        )
        return NormalizedResult(columns=out_columns, rows=out_rows, truncated=truncated, row_cap=row_cap)


__all__ = [
    "DEFAULT_ROW_CAP",
    "ColumnStoreEngine",
    "ColumnStoreTable",
    "InMemoryEngine",
    "QueryEngine",
    "SqliteEngine",
    "guard_read_only",
]
