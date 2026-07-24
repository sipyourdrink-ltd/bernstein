"""End-to-end CLI tests for ``bernstein datasource`` (issue #2887).

Hermetic: the audit key is pointed at a tmp path so the suite never touches the
operator's real XDG state, and every command runs against ``--workdir tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.datasource_cmd import datasource_group


@pytest.fixture(autouse=True)
def _isolated_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _db(tmp_path: Path, rows: list[tuple[int, str]]) -> str:
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return str(path)


def _run(args: list[str]) -> object:
    return CliRunner().invoke(datasource_group, args)


def test_register_query_verify_roundtrip(tmp_path: Path) -> None:
    db = _db(tmp_path, [(1, "a"), (2, "b")])
    wd = ["--workdir", str(tmp_path)]

    reg = _run(["register", "sales", db, *wd])
    assert reg.exit_code == 0, reg.output
    assert "Registered datasource" in reg.output

    q = _run(["query", "sales", "SELECT id, name FROM t ORDER BY id", "--json", *wd])
    assert q.exit_code == 0, q.output
    receipt = json.loads(q.output)
    rid = receipt["receipt_id"]
    assert receipt["row_count"] == 2
    assert receipt["content_hash"].startswith("sha256:")

    v = _run(["verify", rid, *wd])
    assert v.exit_code == 0, v.output
    assert "Receipt verified" in v.output


def test_query_refuses_write(tmp_path: Path) -> None:
    db = _db(tmp_path, [(1, "a")])
    wd = ["--workdir", str(tmp_path)]
    _run(["register", "sales", db, *wd])
    q = _run(["query", "sales", "DELETE FROM t", *wd])
    assert q.exit_code == 1
    assert "Query failed" in q.output


def test_verify_reexecute_match_and_drift(tmp_path: Path) -> None:
    db = _db(tmp_path, [(1, "a"), (2, "b")])
    wd = ["--workdir", str(tmp_path)]
    _run(["register", "sales", db, *wd])
    q = _run(["query", "sales", "SELECT id, name FROM t ORDER BY id", "--json", *wd])
    rid = json.loads(q.output)["receipt_id"]

    ok = _run(["verify", rid, "--re-execute", "--json", *wd])
    assert ok.exit_code == 0, ok.output
    # The JSON block is the last object printed.
    assert '"status": "MATCH"' in ok.output

    conn = sqlite3.connect(db)
    conn.execute("UPDATE t SET name = 'z' WHERE id = 1")
    conn.commit()
    conn.close()

    drift = _run(["verify", rid, "--re-execute", *wd])
    assert drift.exit_code == 1
    assert "DRIFT" in drift.output


def test_list_redacts_and_shows(tmp_path: Path) -> None:
    db = _db(tmp_path, [(1, "a")])
    wd = ["--workdir", str(tmp_path)]
    _run(["register", "sales", db, "--description", "prod sales", *wd])
    out = _run(["list", *wd])
    assert out.exit_code == 0
    assert "sales" in out.output
    assert "prod sales" in out.output


def test_verify_missing_receipt_errors(tmp_path: Path) -> None:
    wd = ["--workdir", str(tmp_path)]
    out = _run(["verify", "sha256:" + "0" * 64, *wd])
    assert out.exit_code == 1
    assert "Cannot verify" in out.output or "FAILED" in out.output
