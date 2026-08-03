"""Canonical SQLite schema snapshots: content digest + per-object drift diff.

A statement's meaning depends on the schema it was written against: the same
``SELECT`` over a table whose columns changed in between is a different
question. The query driver (:mod:`bernstein.core.datasources.query_driver`)
therefore records the schema state a result was derived against as a signed,
content-addressed input. This module supplies that snapshot: a deterministic
projection of the live SQLite schema, its ``sha256:`` content digest, and a
per-object diff that names what changed between two snapshots.

Why a content digest and not ``PRAGMA schema_version``
------------------------------------------------------

SQLite's ``schema_version`` pragma is a write counter, not a content address:
it increments on every schema touch, so a table created and dropped again
leaves the counter changed while the schema is logically identical, and two
databases holding the same schema (a restore, a copy built from the same DDL)
carry unrelated counter values. A receipt needs the opposite property -- equal
content, equal digest -- so the snapshot hashes the schema *content*: the
normalised DDL text SQLite itself keeps in ``sqlite_master`` plus the
structural column list per table.

Determinism
-----------

``sqlite_master`` row order is an engine detail, so objects are ordered
canonically by ``(type, name)`` before encoding. The DDL text is taken
verbatim from ``sqlite_master.sql`` (SQLite normalises it and rewrites it on
``ALTER TABLE``); the snapshot never rewrites DDL, because a lossy rewrite
could collapse two genuinely different schemas onto one digest. Tables
additionally carry their ``PRAGMA table_info`` column rows, so a structural
change is always visible in the diff at column granularity. Internal objects
(``sqlite_%`` names, e.g. ``sqlite_sequence``) are excluded: they are engine
bookkeeping, not operator schema.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.datasources.errors import DataSourceError

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Snapshot format version, folded into the canonical bytes so a future
#: format change can never silently re-hash an old snapshot.
SNAPSHOT_VERSION = 1

#: The object types read from ``sqlite_master``.
_OBJECT_TYPES = ("table", "index", "view", "trigger")


@dataclass(frozen=True, slots=True)
class SchemaColumn:
    """One column of a table, as reported by ``PRAGMA table_info``.

    Attributes:
        name: Column name.
        declared_type: The declared type text (may be empty for typeless columns).
        notnull: Whether the column carries a NOT NULL constraint.
        default: The default-value expression text, or ``None`` when absent.
        primary_key: 1-based position in the primary key, 0 when not part of it.
    """

    name: str
    declared_type: str
    notnull: bool
    default: str | None
    primary_key: int

    def to_dict(self) -> dict[str, object]:
        """Return the JSON projection folded into the canonical bytes."""
        return {
            "name": self.name,
            "declared_type": self.declared_type,
            "notnull": self.notnull,
            "default": self.default,
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> SchemaColumn:
        """Rebuild a column from its canonical projection, refusing malformed fields.

        Every canonical projection was written by :meth:`to_dict`, which always
        emits all five keys with these exact types - so a mismatch here is
        corruption or tampering, never a legitimate legacy blob, and it
        refuses instead of defaulting a field into the trusted digest.

        Raises:
            DataSourceError: A required key is missing or carries the wrong type.
        """
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise DataSourceError("schema column has a missing or empty name")
        declared_type = row.get("declared_type")
        if not isinstance(declared_type, str):
            raise DataSourceError(f"schema column {name!r} has a non-string declared_type")
        notnull = row.get("notnull")
        if not isinstance(notnull, bool):
            raise DataSourceError(f"schema column {name!r} has a non-boolean notnull")
        if "default" not in row:
            raise DataSourceError(
                f"schema column {name!r} is missing its default key; a truncated projection "
                "must not hash equal to a column with no default"
            )
        default = row["default"]
        if default is not None and not isinstance(default, str):
            raise DataSourceError(f"schema column {name!r} has a non-string default")
        primary_key = row.get("primary_key")
        if not isinstance(primary_key, int) or isinstance(primary_key, bool):
            raise DataSourceError(f"schema column {name!r} has a missing or non-integer primary_key")
        return cls(name=name, declared_type=declared_type, notnull=notnull, default=default, primary_key=primary_key)


@dataclass(frozen=True, slots=True)
class SchemaObject:
    """One schema object: a table, index, view, or trigger.

    Attributes:
        type: Object type as reported by ``sqlite_master`` (``table`` /
            ``index`` / ``view`` / ``trigger``).
        name: Object name.
        sql: The normalised DDL text from ``sqlite_master.sql`` (empty when
            SQLite stores none).
        columns: For tables, the ``PRAGMA table_info`` column rows in declared
            order; empty for every other object type.
    """

    type: str
    name: str
    sql: str
    columns: tuple[SchemaColumn, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return the JSON projection folded into the canonical bytes."""
        return {
            "type": self.type,
            "name": self.name,
            "sql": self.sql,
            "columns": [c.to_dict() for c in self.columns],
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> SchemaObject:
        """Rebuild an object from its canonical projection."""
        raw_columns = row.get("columns", [])
        if not isinstance(raw_columns, list):
            raise DataSourceError("stored schema snapshot: schema object columns must be a list")
        for entry in cast("list[Any]", raw_columns):
            if not isinstance(entry, dict):
                raise DataSourceError("stored schema snapshot: schema object carries a non-object column entry")
        columns = tuple(SchemaColumn.from_dict(cast("dict[str, Any]", c)) for c in cast("list[Any]", raw_columns))
        valid_type, valid_name, valid_sql = _validated_object_shape(
            row.get("type"), row.get("name"), row.get("sql"), origin="stored schema snapshot"
        )
        return cls(type=valid_type, name=valid_name, sql=valid_sql, columns=columns)


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    """A canonical, content-addressed snapshot of a SQLite schema.

    Attributes:
        objects: Schema objects in canonical ``(type, name)`` order.
    """

    objects: tuple[SchemaObject, ...]

    def canonical_bytes(self) -> bytes:
        """Return the deterministic canonical byte encoding of the snapshot.

        Canonical JSON (sorted keys, minimal separators, UTF-8) over the
        version tag and the canonically-ordered objects. Two databases holding
        the same schema encode to byte-identical output regardless of the
        order ``sqlite_master`` returned rows in.
        """
        payload = {
            "v": SNAPSHOT_VERSION,
            "objects": [o.to_dict() for o in self.objects],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return ``sha256:<hex>`` over :meth:`canonical_bytes`."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, data: bytes) -> SchemaSnapshot:
        """Rebuild a snapshot from its canonical bytes (e.g. a stored input blob).

        Raises:
            DataSourceError: When the bytes are not a valid snapshot encoding.
        """
        try:
            raw: Any = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DataSourceError(f"schema snapshot bytes are not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise DataSourceError("schema snapshot bytes carry an unknown format version")
        payload = cast("dict[str, Any]", raw)
        if payload.get("v") != SNAPSHOT_VERSION:
            raise DataSourceError("schema snapshot bytes carry an unknown format version")
        raw_objects = payload.get("objects", [])
        if not isinstance(raw_objects, list):
            raise DataSourceError("schema snapshot bytes are malformed (objects is not a list)")
        for position, entry in enumerate(cast("list[Any]", raw_objects)):
            if not isinstance(entry, dict):
                raise DataSourceError(
                    f"stored schema snapshot: objects[{position}] is not an object; refusing to "
                    "rehydrate a snapshot by omission"
                )
        objects = tuple(SchemaObject.from_dict(cast("dict[str, Any]", o)) for o in cast("list[Any]", raw_objects))
        return cls(objects=_canonical_order(objects))


@dataclass(frozen=True, slots=True)
class SchemaObjectDrift:
    """One named schema difference between a recorded and a live snapshot.

    Attributes:
        change: ``added`` / ``removed`` / ``changed`` (live relative to recorded).
        object_type: The object's ``sqlite_master`` type.
        name: The object's name.
        detail: A short human-readable description of what changed (for tables,
            the added / removed / redefined column names).
    """

    change: str
    object_type: str
    name: str
    detail: str = ""

    def describe(self) -> str:
        """Return a one-line description, e.g. ``changed table 'orders': ...``."""
        base = f"{self.change} {self.object_type} {self.name!r}"
        return f"{base}: {self.detail}" if self.detail else base


def _quote_ident(ident: str) -> str:
    """Quote a SQLite identifier for interpolation into a PRAGMA."""
    return '"' + ident.replace('"', '""') + '"'


def _validated_object_shape(obj_type: object, name: object, sql: object, *, origin: str) -> tuple[str, str, str]:
    """One shape rule for every digest-bearing :class:`SchemaObject` producer.

    Both producers (the live ``sqlite_master`` scan and rehydration of a
    stored snapshot) route through this check, so a malformed field is a
    typed refusal at either boundary instead of a silent empty-string
    default entering the trusted digest.
    """
    if not isinstance(obj_type, str) or obj_type not in _OBJECT_TYPES:
        raise DataSourceError(f"{origin}: schema object type must be one of {_OBJECT_TYPES}, got {obj_type!r}")
    if not isinstance(name, str) or not name:
        raise DataSourceError(f"{origin}: schema object {obj_type} has a missing or empty name")
    if sql is not None and not isinstance(sql, str):
        raise DataSourceError(f"{origin}: schema object {name!r} has a non-string sql field")
    return obj_type, name, sql or ""


def _canonical_order(objects: tuple[SchemaObject, ...]) -> tuple[SchemaObject, ...]:
    """Order objects canonically by ``(type, name)``."""
    return tuple(sorted(objects, key=lambda o: (o.type, o.name)))


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[SchemaColumn, ...]:
    """Read the ``PRAGMA table_info`` column rows for *table* in declared order."""
    columns: list[SchemaColumn] = []
    for _cid, name, declared, notnull, default, pk in conn.execute(f"PRAGMA table_info({_quote_ident(table)})"):
        columns.append(
            SchemaColumn(
                name=str(name),
                declared_type=str(declared or ""),
                notnull=bool(notnull),
                default=None if default is None else str(default),
                primary_key=int(pk),
            )
        )
    return tuple(columns)


def snapshot_schema(conn: sqlite3.Connection) -> SchemaSnapshot:
    """Take a canonical schema snapshot over an open SQLite connection.

    Reads every user object from ``sqlite_master`` (internal ``sqlite_%``
    objects are excluded), attaches the ``PRAGMA table_info`` column rows for
    tables, and orders everything canonically so the snapshot's digest is a
    pure function of schema content.

    All reads run inside one read transaction pinned on ``conn`` before the
    first row is fetched: the ``sqlite_master`` scan and every per-table
    ``PRAGMA table_info`` see the same schema state, so a concurrent writer
    (WAL mode) landing an ``ALTER TABLE`` between the reads can never yield a
    digest describing a schema that existed at no point in time.

    Args:
        conn: An open connection (read-only is sufficient).

    Returns:
        The canonical :class:`SchemaSnapshot`.

    Raises:
        DataSourceError: When the schema cannot be read, or the read
            transaction cannot be pinned.
    """
    started_txn = False
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN")
        except sqlite3.DatabaseError as exc:
            raise DataSourceError(f"cannot pin a schema read transaction: {exc}") from exc
        started_txn = True
    try:
        try:
            rows = conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise DataSourceError(f"cannot read schema: {exc}") from exc
        objects: list[SchemaObject] = []
        for obj_type, name, sql in rows:
            if obj_type not in _OBJECT_TYPES:
                continue
            valid_type, valid_name, valid_sql = _validated_object_shape(obj_type, name, sql, origin="live schema")
            columns = _table_columns(conn, valid_name) if valid_type == "table" else ()
            objects.append(SchemaObject(type=valid_type, name=valid_name, sql=valid_sql, columns=columns))
        return SchemaSnapshot(objects=_canonical_order(tuple(objects)))
    finally:
        if started_txn:
            conn.rollback()


def _column_drift_detail(recorded: SchemaObject, live: SchemaObject) -> str:
    """Describe a changed table at column granularity."""
    recorded_cols = {c.name: c for c in recorded.columns}
    live_cols = {c.name: c for c in live.columns}
    added = sorted(set(live_cols) - set(recorded_cols))
    removed = sorted(set(recorded_cols) - set(live_cols))
    redefined = sorted(name for name in set(recorded_cols) & set(live_cols) if recorded_cols[name] != live_cols[name])
    parts: list[str] = []
    if added:
        parts.append("added columns " + ", ".join(repr(c) for c in added))
    if removed:
        parts.append("removed columns " + ", ".join(repr(c) for c in removed))
    if redefined:
        parts.append("redefined columns " + ", ".join(repr(c) for c in redefined))
    if not parts and recorded.sql != live.sql:
        parts.append("definition changed")
    return "; ".join(parts) or "definition changed"


def diff_snapshots(recorded: SchemaSnapshot, live: SchemaSnapshot) -> tuple[SchemaObjectDrift, ...]:
    """Name every object that differs between *recorded* and *live*.

    The diff is keyed by ``(type, name)``: an object present only in *live* is
    ``added``, only in *recorded* is ``removed``, and present in both with a
    different DDL text or column list is ``changed`` (with column-level detail
    for tables). An empty result means the snapshots hold the same content
    (and therefore the same digest).

    Args:
        recorded: The snapshot a result was recorded against.
        live: The snapshot of the schema as it is now.

    Returns:
        The named drifts in canonical ``(type, name)`` order; empty when equal.
    """
    recorded_by_key = {(o.type, o.name): o for o in recorded.objects}
    live_by_key = {(o.type, o.name): o for o in live.objects}
    drifts: list[SchemaObjectDrift] = []
    for key in sorted(set(recorded_by_key) | set(live_by_key)):
        obj_type, name = key
        rec = recorded_by_key.get(key)
        cur = live_by_key.get(key)
        if rec is None and cur is not None:
            drifts.append(SchemaObjectDrift(change="added", object_type=obj_type, name=name))
        elif cur is None and rec is not None:
            drifts.append(SchemaObjectDrift(change="removed", object_type=obj_type, name=name))
        elif rec is not None and cur is not None and rec != cur:
            drifts.append(
                SchemaObjectDrift(
                    change="changed",
                    object_type=obj_type,
                    name=name,
                    detail=_column_drift_detail(rec, cur),
                )
            )
    return tuple(drifts)


__all__ = [
    "SNAPSHOT_VERSION",
    "SchemaColumn",
    "SchemaObject",
    "SchemaObjectDrift",
    "SchemaSnapshot",
    "diff_snapshots",
    "snapshot_schema",
]
