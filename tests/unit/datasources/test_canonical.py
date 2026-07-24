"""Canonical encoding + cross-engine determinism (issue #2887).

Covers acceptance criteria:

* AC1 - the same logical query on static data produces a byte-identical
  ``content_hash`` across two runs and across two independent engines for
  type-equivalent fixtures.
* AC4 - a truncated result can never verify as an untruncated one because the
  truncation flag is inside the hashed body.
* Edge vectors - NULL, decimal scale, unicode NFC, blob binary safety.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from bernstein.core.datasources import result as R
from bernstein.core.datasources.engine import (
    ColumnStoreEngine,
    ColumnStoreTable,
    InMemoryEngine,
    SqliteEngine,
)
from bernstein.core.datasources.errors import NonCanonicalText, UnsupportedValue
from bernstein.core.datasources.result import (
    NormalizedColumn,
    NormalizedResult,
    canonical_bytes,
    content_hash,
)


def _mk(columns: list[tuple[str, str]], rows: list[tuple[object, ...]], **kw: object) -> NormalizedResult:
    return NormalizedResult(
        columns=tuple(NormalizedColumn(name=n, type=t) for n, t in columns),
        rows=tuple(rows),
        **kw,  # type: ignore[arg-type]
    )


# --- determinism ------------------------------------------------------------


def test_same_result_hashes_identically_across_two_runs() -> None:
    r1 = _mk([("id", R.INTEGER), ("name", R.TEXT)], [(1, "a"), (2, "b")])
    r2 = _mk([("id", R.INTEGER), ("name", R.TEXT)], [(1, "a"), (2, "b")])
    assert content_hash(r1) == content_hash(r2)


def test_row_order_is_significant() -> None:
    a = _mk([("id", R.INTEGER)], [(1,), (2,)])
    b = _mk([("id", R.INTEGER)], [(2,), (1,)])
    assert content_hash(a) != content_hash(b)


def test_column_order_is_significant() -> None:
    a = _mk([("x", R.INTEGER), ("y", R.INTEGER)], [(1, 2)])
    b = _mk([("y", R.INTEGER), ("x", R.INTEGER)], [(1, 2)])
    assert content_hash(a) != content_hash(b)


# --- cross-engine byte identity (AC1) ---------------------------------------


def _sqlite_fixture(tmp_path: Path) -> str:
    db = tmp_path / "fixture.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE metrics (id INTEGER, label TEXT, ratio REAL, flag BOOLEAN, note TEXT, raw BLOB)")
    conn.executemany(
        "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "alpha", 0.25, 1, "x", b"\x00\x01"),
            (2, "beta", 0.5, 0, None, b"ff"),
            (3, "gamma", 1.0, 1, "z", b""),
        ],
    )
    conn.commit()
    conn.close()
    return str(db)


_FIXTURE_SQL = "SELECT id, label, ratio, flag, note, raw FROM metrics ORDER BY id"


def test_sqlite_and_in_memory_engines_agree(tmp_path: Path) -> None:
    db = _sqlite_fixture(tmp_path)
    sqlite_result = SqliteEngine(db).execute(_FIXTURE_SQL)

    in_memory = InMemoryEngine(
        columns=[
            ("id", R.INTEGER),
            ("label", R.TEXT),
            ("ratio", R.FLOAT),
            ("flag", R.BOOLEAN),
            ("note", R.TEXT),
            ("raw", R.BLOB),
        ],
        rows=[
            (1, "alpha", 0.25, True, "x", b"\x00\x01"),
            (2, "beta", 0.5, False, None, b"ff"),
            (3, "gamma", 1.0, True, "z", b""),
        ],
    )
    ref_result = in_memory.execute("ignored")

    assert content_hash(sqlite_result) == content_hash(ref_result)


def test_sqlite_numeric_column_is_homogeneous_float(tmp_path: Path) -> None:
    # A NUMERIC/DECIMAL column loses fixed scale (SQLite has no decimal type);
    # the reference engine surfaces it as a homogeneous float column so cross-run
    # identity holds even though "2.00" and "0.00" arrive as integer-affinity 2/0.
    db = tmp_path / "numeric.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE m (amount DECIMAL)")
    conn.executemany("INSERT INTO m VALUES (?)", [("1.50",), ("2.00",), ("0.00",)])
    conn.commit()
    conn.close()
    res = SqliteEngine(str(db)).execute("SELECT amount FROM m ORDER BY amount")
    assert res.columns[0].type == R.FLOAT
    assert [type(v).__name__ for (v,) in res.rows] == ["float", "float", "float"]

    mirror = InMemoryEngine(columns=[("amount", R.FLOAT)], rows=[(0.0,), (1.5,), (2.0,)]).execute("x")
    assert content_hash(res) == content_hash(mirror)


def test_sqlite_content_hash_is_stable_across_runs(tmp_path: Path) -> None:
    db = _sqlite_fixture(tmp_path)
    h1 = content_hash(SqliteEngine(db).execute(_FIXTURE_SQL))
    h2 = content_hash(SqliteEngine(db).execute(_FIXTURE_SQL))
    assert h1 == h2


# --- cross-engine reconciliation, two real engines (AC1) --------------------
#
# ``InMemoryEngine`` above proves the sqlite adapter matches a hand-written
# NormalizedResult spec. It does not prove that a *second, genuinely different*
# engine -- one with its own type vocabulary, its own cell coercion, and its own
# query executor -- reconciles raw source data onto byte-identical canonical
# output. ``ColumnStoreEngine`` closes that: it is fed the *same raw fixture*
# sqlite is loaded from (flags as ``0``/``1`` ints, blobs as raw bytes), carries
# an Arrow-style logical type system (``int64``/``utf8``/``float64``/``bool``/
# ``binary``), and runs the *same SQL string* through its own projection + ORDER
# BY executor. Identical ``content_hash`` is then a property proven across two
# independent backends, not a hand-fed mirror.


def test_sqlite_and_column_store_engines_agree(tmp_path: Path) -> None:
    db = _sqlite_fixture(tmp_path)
    sqlite_result = SqliteEngine(db).execute(_FIXTURE_SQL)

    # Physical column order deliberately differs from the SELECT list and the
    # rows are supplied out of id order: the executor must both reorder the
    # projection and sort, so a match cannot be a pass-through of pre-shaped
    # data. Values are raw (flag 0/1, not bool) -- the column store coerces them
    # through its own type path, independently of sqlite.
    store = ColumnStoreEngine(
        {
            "metrics": ColumnStoreTable(
                columns=[
                    ("raw", "binary"),
                    ("note", "large_utf8"),
                    ("flag", "bool"),
                    ("ratio", "float64"),
                    ("label", "utf8"),
                    ("id", "int64"),
                ],
                rows=[
                    (b"ff", None, 0, 0.5, "beta", 2),
                    (b"\x00\x01", "x", 1, 0.25, "alpha", 1),
                    (b"", "z", 1, 1.0, "gamma", 3),
                ],
            )
        }
    )
    store_result = store.execute(_FIXTURE_SQL)

    assert content_hash(sqlite_result) == content_hash(store_result)


def test_column_store_executor_actually_projects_and_sorts() -> None:
    # Guards against the match above being an accident of input ordering: the
    # store engine really evaluates the projection order and ORDER BY.
    store = ColumnStoreEngine(
        {
            "t": ColumnStoreTable(
                columns=[("a", "int64"), ("b", "utf8")],
                rows=[(3, "c"), (1, "a"), (2, "b")],
            )
        }
    )
    res = store.execute("SELECT b, a FROM t ORDER BY a")
    assert [c.name for c in res.columns] == ["b", "a"]
    assert res.rows == (("a", 1), ("b", 2), ("c", 3))


def test_column_store_preserves_decimal_scale() -> None:
    # sqlite cannot attest a fixed-scale decimal (NUMERIC affinity destroys the
    # scale on insert). A decimal-carrying engine can: fed raw strings, the
    # column store surfaces true ``Decimal`` cells whose scale survives into the
    # hash -- the concrete, in-repo demonstration of the "any decimal-carrying
    # engine" claim the sqlite NUMERIC note makes.
    store = ColumnStoreEngine(
        {
            "m": ColumnStoreTable(
                columns=[("amount", "decimal128")],
                rows=[("0.00",), ("1.50",), ("2.00",)],
            )
        }
    )
    res = store.execute("SELECT amount FROM m")
    assert res.columns[0].type == R.DECIMAL
    assert [format(v, "f") for (v,) in res.rows] == ["0.00", "1.50", "2.00"]
    # Mirror against a hand-specified decimal result (same engine row_cap): the
    # raw strings reconcile onto scale-preserving Decimals byte-for-byte.
    mirror = InMemoryEngine(
        columns=[("amount", R.DECIMAL)],
        rows=[(Decimal("0.00"),), (Decimal("1.50"),), (Decimal("2.00"),)],
    ).execute("x")
    assert content_hash(res) == content_hash(mirror)


def test_column_store_refuses_write() -> None:
    from bernstein.core.datasources.errors import ReadOnlyViolation

    store = ColumnStoreEngine({"t": ColumnStoreTable(columns=[("a", "int64")], rows=[(1,)])})
    with pytest.raises(ReadOnlyViolation):
        store.execute("DELETE FROM t")


def test_column_store_row_cap_truncates() -> None:
    store = ColumnStoreEngine({"t": ColumnStoreTable(columns=[("a", "int64")], rows=[(1,), (2,), (3,)])})
    res = store.execute("SELECT a FROM t ORDER BY a", row_cap=2)
    assert res.truncated is True
    assert res.row_count == 2
    assert res.row_cap == 2


def test_column_store_unsupported_query_shape_rejected() -> None:
    from bernstein.core.datasources.errors import UnsupportedStatement

    store = ColumnStoreEngine({"t": ColumnStoreTable(columns=[("a", "int64")], rows=[(1,)])})
    with pytest.raises(UnsupportedStatement):
        store.execute("SELECT a FROM t GROUP BY a")


# --- truncation (AC4) -------------------------------------------------------


def test_truncated_flag_changes_the_hash() -> None:
    full = _mk([("id", R.INTEGER)], [(1,), (2,)], truncated=False, row_cap=0)
    trunc = _mk([("id", R.INTEGER)], [(1,), (2,)], truncated=True, row_cap=2)
    assert content_hash(full) != content_hash(trunc)


def test_truncated_prefix_never_matches_untruncated_original(tmp_path: Path) -> None:
    db = tmp_path / "big.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    conn.commit()
    conn.close()
    sql = "SELECT id FROM t ORDER BY id"

    full = SqliteEngine(str(db)).execute(sql, row_cap=100)
    capped = SqliteEngine(str(db)).execute(sql, row_cap=2)

    assert full.truncated is False
    assert capped.truncated is True
    assert capped.row_count == 2
    assert content_hash(full) != content_hash(capped)


# --- edge vectors -----------------------------------------------------------


def test_null_sentinel_distinct_from_text_null() -> None:
    a = _mk([("v", R.TEXT)], [(None,)])
    b = _mk([("v", R.TEXT)], [("NULL",)])
    assert content_hash(a) != content_hash(b)


def test_null_sentinel_distinct_from_empty_text() -> None:
    a = _mk([("v", R.TEXT)], [(None,)])
    b = _mk([("v", R.TEXT)], [("",)])
    assert content_hash(a) != content_hash(b)


def test_decimal_scale_is_preserved() -> None:
    a = _mk([("v", R.DECIMAL)], [(Decimal("1.50"),)])
    b = _mk([("v", R.DECIMAL)], [(Decimal("1.5"),)])
    assert content_hash(a) != content_hash(b)


def test_integer_and_boolean_do_not_collide() -> None:
    a = _mk([("v", R.INTEGER)], [(1,)])
    b = _mk([("v", R.BOOLEAN)], [(True,)])
    assert content_hash(a) != content_hash(b)


def test_float_shortest_round_trip() -> None:
    # 0.1 has a shortest repr; a value with the same float identity hashes same.
    a = _mk([("v", R.FLOAT)], [(0.1,)])
    b = _mk([("v", R.FLOAT)], [(0.1,)])
    assert content_hash(a) == content_hash(b)


def test_blob_binary_safe_including_delimiters() -> None:
    a = _mk([("v", R.BLOB)], [(b"2:ab",)])
    b = _mk([("v", R.BLOB)], [(b"3:ab",)])
    assert content_hash(a) != content_hash(b)


def test_non_nfc_text_is_rejected() -> None:
    # U+00C5 (composed) vs U+0041 U+030A (decomposed A + ring). The decomposed
    # form is not NFC and must be rejected rather than silently normalised.
    decomposed = "Å"
    with pytest.raises(NonCanonicalText):
        canonical_bytes(_mk([("v", R.TEXT)], [(decomposed,)]))


def test_non_nfc_column_name_is_rejected() -> None:
    decomposed = "Å"
    with pytest.raises(NonCanonicalText):
        canonical_bytes(_mk([(decomposed, R.TEXT)], [("x",)]))


def test_nfc_text_is_accepted_and_stable() -> None:
    composed = "Ångstrom"
    a = _mk([("v", R.TEXT)], [(composed,)])
    b = _mk([("v", R.TEXT)], [(composed,)])
    assert content_hash(a) == content_hash(b)


def test_unsupported_value_type_rejected() -> None:
    with pytest.raises(UnsupportedValue):
        canonical_bytes(_mk([("v", R.TEXT)], [(object(),)]))


def test_row_arity_mismatch_rejected() -> None:
    with pytest.raises(UnsupportedValue):
        canonical_bytes(_mk([("a", R.INTEGER), ("b", R.INTEGER)], [(1,)]))
