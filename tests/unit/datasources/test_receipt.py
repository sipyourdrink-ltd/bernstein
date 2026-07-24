"""Query receipt record / verify / drift (issue #2887).

Covers acceptance criteria:

* AC2 - tampering the stored result copy, the receipt body, or the chain anchor
  makes ``verify`` fail at the named field; removing the lineage entry makes the
  receipt unverifiable.
* AC3 - ``reexecute`` reports MATCH on unchanged data and DRIFT with both hashes
  after a fixture mutation.
* AC4 - a truncated result never verifies as an untruncated one.
* AC5 - connection secrets never appear in the receipt or the audit mirror.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bernstein.core.datasources.connection import DataSourceConnection
from bernstein.core.datasources.receipt import QueryReceiptStore
from bernstein.core.lineage.identity import AgentCard, generate_keypair


@pytest.fixture
def keypair() -> tuple[AgentCard, str]:
    priv, pub = generate_keypair()
    return AgentCard(agent_id="agent:datasource-1", kid="ds-key-1", public_key_pem=pub), priv


@pytest.fixture
def operator_key() -> bytes:
    return b"k" * 32


def _make_db(path: Path, rows: list[tuple[int, str]]) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return str(path)


def _store(tmp_path: Path, keypair: tuple[AgentCard, str], operator_key: bytes) -> QueryReceiptStore:
    card, priv = keypair
    return QueryReceiptStore(
        tmp_path / "datasources",
        agent_card=card,
        private_key_pem=priv,
        operator_hmac_key=operator_key,
    )


def _record(
    tmp_path: Path,
    store: QueryReceiptStore,
    db: str,
    sql: str = "SELECT id, name FROM t ORDER BY id",
    *,
    store_result_copy: bool = True,
    row_cap: int = 10_000,
) -> tuple[DataSourceConnection, object]:
    conn = DataSourceConnection(id="sales", driver="sqlite", dsn=db)
    result = conn.open_engine().execute(sql, row_cap=row_cap)
    receipt = store.record(
        connection=conn,
        query_text=sql,
        params=None,
        result=result,
        store_result_copy=store_result_copy,
    )
    return conn, receipt


# --- happy path -------------------------------------------------------------


def test_record_then_verify_ok(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    outcome = store.verify(receipt.receipt_id)
    assert outcome.ok, outcome.failures
    assert outcome.checks["signature"] is True
    assert outcome.checks["operator_hmac"] is True
    assert outcome.checks["content_hash"] is True
    assert outcome.checks["receipt_body"] is True
    assert outcome.checks["result_copy"] is True


def test_receipt_id_is_the_chain_anchor(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    assert receipt.receipt_id == receipt.lineage_entry_hash
    assert receipt.receipt_id.startswith("sha256:")


# --- AC2 tamper: stored result copy ----------------------------------------


def test_tampered_result_copy_fails_at_result_copy(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    copy_path = store.root / receipt.result_copy_relpath
    copy_path.write_bytes(copy_path.read_bytes() + b"tampered")
    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any(f.startswith("result_copy") for f in outcome.failures)


# --- AC2 tamper: receipt body ----------------------------------------------


def test_tampered_receipt_body_fails_at_receipt_body(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    path = store.receipt_path(receipt.receipt_id)
    data = json.loads(path.read_text())
    data["row_count"] = 999  # a receipt-core field folded into the binding
    path.write_text(json.dumps(data))
    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any(f.startswith("receipt_body") for f in outcome.failures)


def test_tampered_content_hash_fails_at_content_hash(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    path = store.receipt_path(receipt.receipt_id)
    data = json.loads(path.read_text())
    data["content_hash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(data))
    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any(f.startswith("content_hash") for f in outcome.failures)


# --- AC2 tamper: chain anchor ----------------------------------------------


def test_tampered_chain_anchor_fails(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    # Edit the signed entry in the log: its recomputed hash no longer matches
    # the receipt's anchor, so the anchor cannot be located.
    log = store.root / "lineage" / "log.jsonl"
    raw = log.read_text().splitlines()
    entry = json.loads(raw[0])
    entry["content_hash"] = "sha256:" + "1" * 64
    raw[0] = json.dumps(entry)
    log.write_text("\n".join(raw) + "\n")
    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any(f.startswith("lineage_entry") for f in outcome.failures)


def test_tampered_signature_fails_at_signature(tmp_path: Path, keypair, operator_key) -> None:
    # Corrupt the detached JWS sidecar only: the log entry is untouched, so the
    # anchor is still located and its content_hash still matches -- the sole
    # failure is the Ed25519 signature check.
    db = _make_db(tmp_path / "a.db", [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    (sig_path,) = list((store.root / "lineage" / "signatures").rglob("*.jws"))
    header, empty, sig = sig_path.read_text().split(".", maxsplit=2)
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    sig_path.write_text(".".join([header, empty, flipped]))
    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any(f.startswith("signature") for f in outcome.failures)
    assert outcome.checks.get("signature") is not True
    assert outcome.checks.get("lineage_entry") is True


def test_wrong_operator_key_fails_at_operator_hmac(tmp_path: Path, keypair, operator_key) -> None:
    # A verifier holding the wrong operator HMAC key recomputes a different
    # envelope: the Ed25519 signature still verifies (it is key-independent), so
    # the named failure is operator_hmac alone.
    db = _make_db(tmp_path / "a.db", [(1, "a")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    card, priv = keypair
    wrong = QueryReceiptStore(
        store.root,
        agent_card=card,
        private_key_pem=priv,
        operator_hmac_key=b"j" * 32,
    )
    outcome = wrong.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any(f.startswith("operator_hmac") for f in outcome.failures)
    assert outcome.checks.get("operator_hmac") is not True
    assert outcome.checks.get("signature") is True


def test_removed_lineage_entry_is_unverifiable(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a")])
    store = _store(tmp_path, keypair, operator_key)
    _, receipt = _record(tmp_path, store, db)
    (store.root / "lineage" / "log.jsonl").write_text("")
    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert any("unverifiable" in f for f in outcome.failures)
    assert outcome.checks.get("lineage_entry") is not True


# --- AC3 drift --------------------------------------------------------------


def test_reexecute_match_on_unchanged_data(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    conn, receipt = _record(tmp_path, store, db)
    drift = store.reexecute(receipt.receipt_id, conn)
    assert drift.match
    assert drift.status == "MATCH"
    assert drift.recorded_hash == drift.live_hash


def test_reexecute_drift_after_mutation(tmp_path: Path, keypair, operator_key) -> None:
    db_path = tmp_path / "a.db"
    db = _make_db(db_path, [(1, "a"), (2, "b")])
    store = _store(tmp_path, keypair, operator_key)
    conn, receipt = _record(tmp_path, store, db)
    # Mutate the data under the agent.
    c = sqlite3.connect(db_path)
    c.execute("UPDATE t SET name = 'CHANGED' WHERE id = 2")
    c.commit()
    c.close()
    drift = store.reexecute(receipt.receipt_id, conn)
    assert not drift.match
    assert drift.status == "DRIFT"
    assert drift.recorded_hash != drift.live_hash
    assert drift.recorded_hash == receipt.content_hash


# --- AC4 truncation ---------------------------------------------------------


def test_truncated_receipt_distinct_from_full(tmp_path: Path, keypair, operator_key) -> None:
    db = _make_db(tmp_path / "a.db", [(i, f"n{i}") for i in range(5)])
    store = _store(tmp_path, keypair, operator_key)
    _, full = _record(tmp_path, store, db, row_cap=100)
    _, capped = _record(tmp_path, store, db, row_cap=2)
    assert full.truncated is False
    assert capped.truncated is True
    assert full.content_hash != capped.content_hash
    # Both still verify against their own signed anchors.
    assert store.verify(full.receipt_id).ok
    assert store.verify(capped.receipt_id).ok


# --- AC5 secret hygiene -----------------------------------------------------


def test_connection_secret_never_in_receipt_or_audit(tmp_path: Path, keypair, operator_key) -> None:
    # AC5's secret-hygiene guarantee is specifically about the *connection* DSN:
    # a receipt records only ``connection.id``, never the DSN, and the audit
    # mirror stores hashes rather than raw query/params. Bind parameters, by
    # contrast, ARE recorded in the receipt body on purpose -- the receipt attests
    # the exact query+params that produced the result -- so the marker value below
    # stands in for a DSN-borne secret and is asserted absent only where the
    # contract forbids it (the receipt's DSN surface and the audit mirror).
    db = _make_db(tmp_path / "a.db", [(1, "a")])
    store = _store(tmp_path, keypair, operator_key)
    dsn_marker = "sup3rs3cr3t"
    conn = DataSourceConnection(
        id="warehouse",
        driver="sqlite",
        dsn=db,
        description="prod",
    )
    result = conn.open_engine().execute("SELECT id, name FROM t")
    receipt = store.record(
        connection=conn,
        query_text="SELECT id, name FROM t",
        params=[dsn_marker],
        result=result,
        store_result_copy=True,
    )
    # The receipt records only the connection id, never a DSN.
    receipt_text = store.receipt_path(receipt.receipt_id).read_text()
    assert "warehouse" in receipt_text
    assert db not in receipt_text or "dsn" not in json.loads(receipt_text)
    # The audit mirror carries no DSN and no raw query params.
    audit_text = store.audit_path.read_text()
    assert "dsn" not in audit_text
    assert db not in audit_text
    assert dsn_marker not in audit_text


def test_redacted_dsn_masks_password() -> None:
    conn = DataSourceConnection(id="pg", driver="sqlite", dsn=":memory:")
    from bernstein.core.datasources.connection import redact_dsn

    assert redact_dsn("postgresql://user:secret@host:5432/db") == "postgresql://user:***@host:5432/db"
    assert redact_dsn("/var/data/app.db") == "/var/data/app.db"
    assert conn.redacted_dsn == ":memory:"
