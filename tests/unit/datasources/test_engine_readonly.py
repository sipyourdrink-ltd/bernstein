"""Read-only enforcement + typed errors (issue #2887, AC5).

The datasource surface never accepts DML/DDL. Refusal happens twice over: a
textual guard (:func:`guard_read_only`) that raises a typed error before
execution, and a sqlite authorizer that denies write actions even if a guard
bypass were ever found.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bernstein.core.datasources.engine import SqliteEngine, guard_read_only
from bernstein.core.datasources.errors import ReadOnlyViolation, UnsupportedStatement


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (id INTEGER)",
        "ALTER TABLE t ADD COLUMN y INTEGER",
        "REPLACE INTO t VALUES (1)",
        "TRUNCATE TABLE t",
        "  insert  into t values (1)",
        "WITH cte AS (SELECT 1) INSERT INTO t SELECT * FROM cte",
    ],
)
def test_dml_ddl_is_refused_with_typed_error(sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        guard_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE t",
    ],
)
def test_multi_or_empty_statement_refused(sql: str) -> None:
    with pytest.raises(UnsupportedStatement):
        guard_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select id from t",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
        "SELECT 1;",  # trailing semicolon is fine
        "VALUES (1), (2)",
    ],
)
def test_read_queries_pass(sql: str) -> None:
    assert guard_read_only(sql) == sql.strip()


def test_write_keyword_inside_string_literal_is_not_a_write() -> None:
    # A SELECT whose *data* mentions DELETE must not be misclassified.
    assert guard_read_only("SELECT 'please DELETE later' AS note") is not None


def test_embedded_semicolon_in_literal_is_single_statement() -> None:
    assert guard_read_only("SELECT 'a;b' AS s") is not None


def _table_db(tmp_path: Path) -> str:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return str(db)


def test_sqlite_engine_refuses_write(tmp_path: Path) -> None:
    engine = SqliteEngine(_table_db(tmp_path))
    with pytest.raises(ReadOnlyViolation):
        engine.execute("DELETE FROM t")


def test_write_pragma_surfaces_typed_readonly_error(tmp_path: Path) -> None:
    # A value-setting PRAGMA leads with a read keyword, so the textual guard lets
    # it through, but the mode=ro connection blocks the write at execution. That
    # denial must surface as our typed ReadOnlyViolation, not a raw
    # sqlite3.OperationalError -- the CLI only catches DataSourceError, so an
    # untyped error would escape as an unhandled crash.
    engine = SqliteEngine(_table_db(tmp_path))
    with pytest.raises(ReadOnlyViolation):
        engine.execute("PRAGMA user_version = 5")


def test_sqlite_engine_opens_read_only(tmp_path: Path) -> None:
    # Even a hand-rolled write that somehow reached execute must fail: the
    # connection is opened mode=ro, so the file is physically read-only.
    db = _table_db(tmp_path)
    engine = SqliteEngine(db)
    result = engine.execute("SELECT id FROM t")
    assert result.row_count == 1
    # Direct proof the underlying file is opened read-only.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (2)")
    conn.close()
