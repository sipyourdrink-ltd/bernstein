"""Canonical SQLite schema snapshots: digest identity and per-object drift.

The snapshot's contract is content-addressing: equal schema content yields an
equal digest regardless of engine row order or object creation order, and any
structural change (a column added, an index dropped) changes the digest and is
named by the diff. Fixtures are built in-test from SQL statements -- no binary
database files -- so CI stays offline and the fixtures stay reviewable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bernstein.core.datasources.errors import DataSourceError
from bernstein.core.datasources.schema import (
    SchemaSnapshot,
    diff_snapshots,
    snapshot_schema,
)

_BASE_DDL = (
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL, note TEXT DEFAULT 'none')",
    "CREATE INDEX idx_orders_amount ON orders(amount)",
    "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)",
)


def _build_db(path: Path, statements: tuple[str, ...]) -> None:
    """Build a fixture database from SQL statements (checked-in, no binary)."""
    conn = sqlite3.connect(path)
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _snapshot(path: Path) -> SchemaSnapshot:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return snapshot_schema(conn)
    finally:
        conn.close()


def test_snapshot_digest_is_stable_across_identical_databases(tmp_path: Path) -> None:
    # Two databases built from the same DDL are the same schema content, so
    # they must carry the same digest -- the property PRAGMA schema_version
    # (a write counter) cannot provide.
    _build_db(tmp_path / "a.db", _BASE_DDL)
    _build_db(tmp_path / "b.db", _BASE_DDL)
    snap_a = _snapshot(tmp_path / "a.db")
    snap_b = _snapshot(tmp_path / "b.db")
    assert snap_a.digest == snap_b.digest
    assert snap_a.digest.startswith("sha256:")
    assert snap_a.canonical_bytes() == snap_b.canonical_bytes()


def test_snapshot_digest_is_independent_of_object_creation_order(tmp_path: Path) -> None:
    # sqlite_master row order is an engine detail; the snapshot orders objects
    # canonically, so creation order cannot leak into the digest.
    _build_db(tmp_path / "ab.db", _BASE_DDL)
    reversed_ddl = (_BASE_DDL[2], _BASE_DDL[0], _BASE_DDL[1])
    _build_db(tmp_path / "ba.db", reversed_ddl)
    assert _snapshot(tmp_path / "ab.db").digest == _snapshot(tmp_path / "ba.db").digest


def test_snapshot_digest_changes_when_column_added(tmp_path: Path) -> None:
    db = tmp_path / "orders.db"
    _build_db(db, _BASE_DDL)
    before = _snapshot(db).digest

    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN discount REAL")
        conn.commit()
    finally:
        conn.close()

    after = _snapshot(db).digest
    assert before != after


def test_snapshot_roundtrips_through_canonical_bytes(tmp_path: Path) -> None:
    # The signed input blob is the canonical bytes; a verifier must be able to
    # rebuild the identical snapshot (and digest) from the stored blob alone.
    db = tmp_path / "orders.db"
    _build_db(db, _BASE_DDL)
    snap = _snapshot(db)
    restored = SchemaSnapshot.from_bytes(snap.canonical_bytes())
    assert restored == snap
    assert restored.digest == snap.digest


def test_snapshot_from_bytes_rejects_malformed_input() -> None:
    with pytest.raises(DataSourceError):
        SchemaSnapshot.from_bytes(b"not json at all")
    with pytest.raises(DataSourceError):
        SchemaSnapshot.from_bytes(b'{"v":999,"objects":[]}')


def test_internal_sqlite_objects_are_excluded(tmp_path: Path) -> None:
    # sqlite_sequence (AUTOINCREMENT bookkeeping) is engine state, not operator
    # schema: it must not appear in the snapshot and must not perturb the digest.
    db = tmp_path / "auto.db"
    _build_db(
        db,
        (
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)",
            "INSERT INTO t (name) VALUES ('a')",
        ),
    )
    snap = _snapshot(db)
    names = [o.name for o in snap.objects]
    assert names == ["t"]


def test_diff_names_added_removed_and_changed_objects(tmp_path: Path) -> None:
    recorded_db = tmp_path / "recorded.db"
    live_db = tmp_path / "live.db"
    _build_db(recorded_db, _BASE_DDL)
    _build_db(live_db, _BASE_DDL)

    conn = sqlite3.connect(live_db)
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN discount REAL")
        conn.execute("DROP INDEX idx_orders_amount")
        conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    drifts = diff_snapshots(_snapshot(recorded_db), _snapshot(live_db))
    by_name = {d.name: d for d in drifts}

    assert by_name["invoices"].change == "added"
    assert by_name["idx_orders_amount"].change == "removed"
    assert by_name["orders"].change == "changed"
    assert "discount" in by_name["orders"].detail
    # The untouched table must not be named as drifted.
    assert "customers" not in by_name


def test_diff_is_empty_for_equal_snapshots(tmp_path: Path) -> None:
    _build_db(tmp_path / "a.db", _BASE_DDL)
    _build_db(tmp_path / "b.db", _BASE_DDL)
    assert diff_snapshots(_snapshot(tmp_path / "a.db"), _snapshot(tmp_path / "b.db")) == ()
