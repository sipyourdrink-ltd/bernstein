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


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda obj: obj.update(name=""), "missing or empty name"),
        (lambda obj: obj.update(type="shadow"), "type must be one of"),
        (lambda obj: obj.update(sql=7), "non-string sql"),
    ],
    ids=["empty-name", "unknown-type", "non-string-sql"],
)
def test_malformed_stored_snapshot_is_refused_not_defaulted(tmp_path: Path, mutate, match: str) -> None:
    # A stored snapshot blob is external input at drift-compare time. A field
    # silently defaulted to "" would enter the trusted digest and turn a
    # malformed blob into a self-consistent-looking comparison baseline; the
    # rehydration boundary must refuse instead.
    import json

    from bernstein.core.datasources.errors import DataSourceError

    _build_db(tmp_path / "a.db", _BASE_DDL)
    payload = json.loads(_snapshot(tmp_path / "a.db").canonical_bytes())
    mutate(payload["objects"][0])
    with pytest.raises(DataSourceError, match=match):
        SchemaSnapshot.from_bytes(json.dumps(payload).encode("utf-8"))


def test_non_object_snapshot_entry_is_refused_not_dropped(tmp_path: Path) -> None:
    # A snapshot rehydrated with an entry silently omitted re-canonicalises to
    # a digest that can match a live schema missing the same object - the
    # refusal keeps a corrupted blob from becoming a valid comparison baseline.
    import json

    from bernstein.core.datasources.errors import DataSourceError

    _build_db(tmp_path / "a.db", _BASE_DDL)
    payload = json.loads(_snapshot(tmp_path / "a.db").canonical_bytes())
    payload["objects"].insert(1, "not-an-object")
    with pytest.raises(DataSourceError, match=r"objects\[1\] is not an object"):
        SchemaSnapshot.from_bytes(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda obj: obj.update(columns="nope"), "columns must be a list"),
        (lambda obj: obj["columns"].append("not-a-dict"), "non-object column entry"),
        (lambda obj: obj["columns"][0].update(name=""), "missing or empty name"),
        (lambda obj: obj["columns"][0].update(primary_key="1"), "non-integer primary_key"),
        (lambda obj: obj["columns"][0].pop("default"), "missing its default key"),
    ],
    ids=["non-list-columns", "non-dict-entry", "empty-column-name", "stringified-primary-key", "omitted-default-key"],
)
def test_malformed_column_entries_are_refused_not_dropped(tmp_path: Path, mutate, match: str) -> None:
    # Pre-fix, a non-dict column entry was silently filtered and a non-list
    # columns value coerced to () - both let a malformed stored blob rehydrate
    # into a snapshot whose digest no longer describes what was stored.
    import json

    from bernstein.core.datasources.errors import DataSourceError

    _build_db(tmp_path / "a.db", _BASE_DDL)
    payload = json.loads(_snapshot(tmp_path / "a.db").canonical_bytes())
    table = next(o for o in payload["objects"] if o["type"] == "table")
    mutate(table)
    with pytest.raises(DataSourceError, match=match):
        SchemaSnapshot.from_bytes(json.dumps(payload).encode("utf-8"))


def test_schema_mutation_during_snapshot_never_yields_a_mixed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The snapshot reads sqlite_master and then PRAGMA table_info per table.
    # A concurrent ALTER TABLE landing between those reads must never produce
    # a digest describing a schema that never existed (old DDL text + new
    # column list). All reads run inside one read transaction, so the
    # snapshot is pinned at its first read: in WAL mode a concurrent writer
    # commits freely and the snapshot still reflects the pre-ALTER state.
    import bernstein.core.datasources.schema as schema_module

    db = tmp_path / "wal.db"
    conn_build = sqlite3.connect(db)
    conn_build.execute("PRAGMA journal_mode=WAL")
    for stmt in _BASE_DDL:
        conn_build.execute(stmt)
    conn_build.commit()
    conn_build.close()

    # Reference digests for the two states that really existed.
    pre_digest = _snapshot(db).digest
    twin = tmp_path / "twin.db"
    conn_twin = sqlite3.connect(twin)
    conn_twin.execute("PRAGMA journal_mode=WAL")
    for stmt in _BASE_DDL:
        conn_twin.execute(stmt)
    conn_twin.execute("ALTER TABLE orders ADD COLUMN discount REAL")
    conn_twin.commit()
    conn_twin.close()
    post_digest = _snapshot(twin).digest
    assert pre_digest != post_digest

    conn_reader = sqlite3.connect(db)
    conn_writer = sqlite3.connect(db)
    real_table_columns = schema_module._table_columns
    fired = {"done": False}

    def alter_then_read(conn: sqlite3.Connection, table: str) -> object:
        # A concurrent writer lands an ALTER between the sqlite_master read
        # and the per-table PRAGMA reads.
        if not fired["done"]:
            fired["done"] = True
            conn_writer.execute("ALTER TABLE orders ADD COLUMN discount REAL")
            conn_writer.commit()
        return real_table_columns(conn, table)

    monkeypatch.setattr(schema_module, "_table_columns", alter_then_read)
    try:
        snap = snapshot_schema(conn_reader)
    finally:
        conn_reader.close()
        conn_writer.close()

    assert fired["done"], "the concurrent ALTER never fired; the test proved nothing"
    # The digest must describe a schema state that actually existed -- with
    # the read transaction pinned before the ALTER, that is the pre state.
    assert snap.digest in {pre_digest, post_digest}
    assert snap.digest == pre_digest
