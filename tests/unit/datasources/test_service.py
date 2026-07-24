"""Service wiring: receipt store provisioning + task-input resolution (#2887)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bernstein.core.datasources.connection import DataSourceConnection
from bernstein.core.datasources.errors import ReadOnlyViolation
from bernstein.core.datasources.service import (
    TaskDatasourceInput,
    build_connection_registry,
    build_receipt_store,
    resolve_task_datasource_inputs,
)


@pytest.fixture(autouse=True)
def _isolated_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _db(tmp_path: Path) -> str:
    path = tmp_path / "d.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])
    conn.commit()
    conn.close()
    return str(path)


def test_build_receipt_store_persists_identity(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = build_receipt_store(sdd)
    assert (sdd / "datasources" / "identity").exists()
    # A second build reuses the same identity, so a receipt recorded once
    # verifies under the store returned by a later call.
    store2 = build_receipt_store(sdd)
    assert store2.root == store.root


def test_resolve_task_inputs_records_receipts(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    db = _db(tmp_path)
    registry = build_connection_registry(sdd)
    registry.put(DataSourceConnection(id="sales", driver="sqlite", dsn=db))

    resolved = resolve_task_datasource_inputs(
        sdd,
        [TaskDatasourceInput(connection_id="sales", query="SELECT id, name FROM t ORDER BY id")],
    )
    assert len(resolved) == 1
    r = resolved[0]
    assert r.row_count == 2
    assert r.content_hash.startswith("sha256:")
    assert "id (integer) | name (text)" in r.prompt_text

    # The recorded receipt verifies against the same sdd store.
    store = build_receipt_store(sdd)
    assert store.verify(r.receipt_id).ok


def test_resolve_task_inputs_refuses_write(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    db = _db(tmp_path)
    registry = build_connection_registry(sdd)
    registry.put(DataSourceConnection(id="sales", driver="sqlite", dsn=db))
    with pytest.raises(ReadOnlyViolation):
        resolve_task_datasource_inputs(
            sdd,
            [TaskDatasourceInput(connection_id="sales", query="DELETE FROM t")],
        )
