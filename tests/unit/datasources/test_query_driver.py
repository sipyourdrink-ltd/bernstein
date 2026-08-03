"""The read-only query driver behind ``DataActivity`` (issue #3125).

Property per test, proven empirically:

* a write statement is refused with a typed error *before the connection
  executes anything* (trace-callback + nonexistent-file evidence), with the
  sqlite authorizer as a second, independently-exercised layer;
* the result is bound to the schema snapshot it was derived against -- the
  snapshot is a signed input recorded before the plan, and its digest moves
  when a column is added;
* the canonical result bytes are a signed output bound to the plan: a one-byte
  mutation of the stored bytes breaks offline verification;
* row order is made explicit by the canonicaliser, so two databases holding
  the same logical rows in different physical order produce byte-identical
  canonical results and (with the same signing key) identical receipt hashes
  across separate ``.sdd`` directories;
* schema drift is a typed, fail-closed refusal naming the changed objects;
* recording an output before a plan raises ``DataOpsPhaseError``;
* a connection secret never enters any recorded byte.

All fixture databases are built in-test from SQL statements -- no binary
fixtures, fully offline.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from bernstein.core.datasources.connection import DataSourceConnection
from bernstein.core.datasources.errors import (
    ReadOnlyViolation,
    SchemaDrift,
    UnsupportedStatement,
    UnsupportedValue,
)
from bernstein.core.datasources.query_driver import (
    ReadOnlyQueryDriver,
    SqliteQueryBackend,
    build_sqlite_query_driver,
    query_input_bytes,
)
from bernstein.core.orchestration.activity_modalities import (
    ContentStore,
    DataOpsPhaseError,
    verify_data_ops_receipt,
)
from bernstein.core.skills.catalog.signature import generate_signer_keypair

_FIXTURE_DDL = (
    "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
    "INSERT INTO t (id, name) VALUES (1, 'ada')",
    "INSERT INTO t (id, name) VALUES (2, 'bob')",
    "INSERT INTO t (id, name) VALUES (3, 'cyd')",
)


def _build_db(path: Path, statements: tuple[str, ...] = _FIXTURE_DDL) -> None:
    """Build a fixture database from SQL statements (checked-in, no binary)."""
    conn = sqlite3.connect(path)
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _driver(
    backend: SqliteQueryBackend,
    store_root: Path,
    keypair: tuple[str, str] | None = None,
    connection_id: str = "test",
) -> ReadOnlyQueryDriver:
    priv, pub = keypair if keypair is not None else generate_signer_keypair()
    return ReadOnlyQueryDriver(
        backend,
        store=ContentStore(store_root),
        private_key_pem=priv,
        public_key_pem=pub,
        connection_id=connection_id,
    )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# AC2: read-only enforced at the boundary, before execution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("INSERT INTO t (id, name) VALUES (99, 'x')", ReadOnlyViolation),
        ("UPDATE t SET name = 'x'", ReadOnlyViolation),
        ("DELETE FROM t", ReadOnlyViolation),
        ("DROP TABLE t", ReadOnlyViolation),
        ("CREATE TABLE u (id INTEGER)", ReadOnlyViolation),
        ("ALTER TABLE t ADD COLUMN extra TEXT", ReadOnlyViolation),
        # Multi-statement string with the write in second position.
        ("SELECT id FROM t; DELETE FROM t", UnsupportedStatement),
        # Write inside a common table expression (read leader, write body).
        ("WITH doomed AS (SELECT id FROM t) DELETE FROM t WHERE id IN (SELECT id FROM doomed)", ReadOnlyViolation),
    ],
)
def test_write_statement_refused_before_connection_executes(
    tmp_path: Path, statement: str, expected: type[Exception]
) -> None:
    # The trace callback records every statement the connection executes; a
    # refused write must leave the trace empty -- the refusal precedes any
    # connection work, it is not a rolled-back execution.
    conn = sqlite3.connect(":memory:")
    for stmt in _FIXTURE_DDL:
        conn.execute(stmt)
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    store_root = tmp_path / "cas"
    driver = _driver(SqliteQueryBackend(connection=conn), store_root)

    with pytest.raises(expected):
        driver.run(statement)

    assert traced == []
    # Fail closed all the way down: no input, plan, or output was recorded.
    assert list(store_root.iterdir()) == []


def test_write_statement_refused_even_when_no_database_exists(tmp_path: Path) -> None:
    # With a database path that cannot even be opened, a write still gets the
    # typed refusal -- proof the guard runs before any connection is opened.
    driver = _driver(SqliteQueryBackend(str(tmp_path / "does-not-exist.db")), tmp_path / "cas")
    with pytest.raises(ReadOnlyViolation):
        driver.run("DELETE FROM t")


def test_write_statement_refused_by_authorizer(tmp_path: Path) -> None:
    # Defense in depth: a value-setting PRAGMA passes the textual guard (read
    # leader, no write keyword) but is still refused at the connection by the
    # read-only open + authorizer, surfacing as the same typed error.
    db = tmp_path / "fixture.db"
    _build_db(db)
    store_root = tmp_path / "cas"
    driver = _driver(SqliteQueryBackend(str(db)), store_root)
    with pytest.raises(ReadOnlyViolation):
        driver.run("PRAGMA user_version = 5")
    # No signed output was recorded for the refused statement.
    run_ok = driver.run("SELECT id, name FROM t")
    assert run_ok.result.row_count == 3


# ---------------------------------------------------------------------------
# AC3: the schema snapshot is a signed input recorded before the plan
# ---------------------------------------------------------------------------


def test_result_binds_to_schema_snapshot_digest(tmp_path: Path) -> None:
    db = tmp_path / "fixture.db"
    _build_db(db)
    store = ContentStore(tmp_path / "cas")
    priv, pub = generate_signer_keypair()
    driver = ReadOnlyQueryDriver(
        SqliteQueryBackend(str(db)), store=store, private_key_pem=priv, public_key_pem=pub, connection_id="sales"
    )

    run = driver.run("SELECT id, name FROM t")

    schema_input_hash = _sha256(run.schema.canonical_bytes())
    input_hashes = [a.content_hash for a in run.receipt.inputs]
    # The snapshot is a signed input...
    assert schema_input_hash in input_hashes
    # ...recorded before the plan was derived: the plan hash is a function of
    # the input hashes, so the snapshot is provably inside the plan's preimage.
    assert schema_input_hash in run.plan.input_hashes
    assert run.receipt.plan.plan_hash == run.plan.plan_hash
    # The query text + parameters are the other signed input.
    assert _sha256(query_input_bytes("SELECT id, name FROM t", None)) in input_hashes

    # Adding a column changes the snapshot digest (and so the signed input).
    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE t ADD COLUMN extra TEXT")
        conn.commit()
    finally:
        conn.close()
    run_after = driver.run("SELECT id, name FROM t")
    assert run_after.schema_digest != run.schema_digest
    assert _sha256(run_after.schema.canonical_bytes()) != schema_input_hash


def test_bound_parameters_are_inside_the_signed_query_input(tmp_path: Path) -> None:
    db = tmp_path / "fixture.db"
    _build_db(db)
    driver = _driver(SqliteQueryBackend(str(db)), tmp_path / "cas")

    run = driver.run("SELECT id, name FROM t WHERE id >= ?", [2])

    assert run.result.row_count == 2
    input_hashes = [a.content_hash for a in run.receipt.inputs]
    assert _sha256(query_input_bytes("SELECT id, name FROM t WHERE id >= ?", [2])) in input_hashes
    verdict = verify_data_ops_receipt(run.receipt, store=ContentStore(tmp_path / "cas"))
    assert verdict.ok


# ---------------------------------------------------------------------------
# The signed query input is type-preserving (baz finding 3705959721)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([1], ["1"]),  # int vs str
        ([1], [1.0]),  # int vs float
        ([1], [True]),  # int vs bool
        (["1"], [b"1"]),  # str vs bytes
        ([Decimal("1")], ["1"]),  # decimal vs its string rendering
        ([Decimal("1.50")], [Decimal("1.5")]),  # scale is significant
        ([datetime.datetime(2026, 1, 1)], ["2026-01-01T00:00:00"]),  # datetime vs ISO string
        ([datetime.date(2026, 1, 1)], [datetime.datetime(2026, 1, 1)]),  # date vs datetime
        ([None], [""]),  # NULL vs empty string
    ],
)
def test_int_and_str_params_produce_distinct_signed_inputs(left: list[object], right: list[object]) -> None:
    # The signed input hash must move whenever a parameter's *type* moves,
    # not only its value -- otherwise two semantically different queries
    # share one content address.
    sql = "SELECT id FROM t WHERE id = ?"
    assert query_input_bytes(sql, left) != query_input_bytes(sql, right)


def test_mapping_params_are_order_insensitive_but_shape_sensitive() -> None:
    sql = "SELECT id FROM t WHERE a = :a AND b = :b"
    # Dict iteration order must not leak into the signed bytes...
    assert query_input_bytes(sql, {"a": 1, "b": 2}) == query_input_bytes(sql, {"b": 2, "a": 1})
    # ...but positional and named parameter shapes are distinct.
    assert query_input_bytes(sql, [1, 2]) != query_input_bytes(sql, {"a": 1, "b": 2})


def test_unsupported_param_type_is_refused_not_stringified() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "1"

    with pytest.raises(UnsupportedValue):
        query_input_bytes("SELECT 1", [Opaque()])


def test_param_type_difference_changes_the_receipt_input_hash(tmp_path: Path) -> None:
    # Driver-level: the same statement bound with 1 (int) and "1" (str) must
    # record different signed query inputs on the receipt.
    db = tmp_path / "fixture.db"
    _build_db(db)
    keypair = generate_signer_keypair()
    run_int = _driver(SqliteQueryBackend(str(db)), tmp_path / "cas_int", keypair).run(
        "SELECT id, name FROM t WHERE id = ?", [1]
    )
    run_str = _driver(SqliteQueryBackend(str(db)), tmp_path / "cas_str", keypair).run(
        "SELECT id, name FROM t WHERE id = ?", ["1"]
    )
    hashes_int = {a.content_hash for a in run_int.receipt.inputs}
    hashes_str = {a.content_hash for a in run_str.receipt.inputs}
    assert hashes_int != hashes_str
    assert run_int.receipt_hash != run_str.receipt_hash


def test_decimal_param_never_fails_after_inputs_are_recorded(tmp_path: Path) -> None:
    # sqlite3 has no registered Decimal adapter: pre-fix, a finite Decimal was
    # accepted by the signed input encoding and only blew up at execute time
    # with a raw ProgrammingError - after the input blobs and plan already
    # existed. The engine now binds a finite Decimal as the exact plain-text
    # rendering the signed encoding tags it with, so a Decimal-parameterised
    # run completes all the way to a signed output instead of failing between
    # input recording and execution.
    db = tmp_path / "fixture.db"
    _build_db(db)
    run = _driver(SqliteQueryBackend(str(db)), tmp_path / "cas_dec").run(
        "SELECT id, name FROM t WHERE id = ?", [Decimal("1")]
    )
    assert run.receipt.inputs
    assert run.result.rows


# ---------------------------------------------------------------------------
# AC4: the canonical result bytes are a signed output; tamper breaks it
# ---------------------------------------------------------------------------


def test_receipt_verifies_offline_and_tamper_fails(tmp_path: Path) -> None:
    db = tmp_path / "fixture.db"
    _build_db(db)
    store = ContentStore(tmp_path / "cas")
    priv, pub = generate_signer_keypair()
    driver = ReadOnlyQueryDriver(
        SqliteQueryBackend(str(db)), store=store, private_key_pem=priv, public_key_pem=pub, connection_id="sales"
    )

    run = driver.run("SELECT id, name FROM t")
    assert verify_data_ops_receipt(run.receipt, store=store).ok

    # Flip one byte of the stored canonical result bytes: the receipt must
    # stop verifying, and the failure must name evidence reattachment.
    (output,) = run.receipt.outputs
    stored = bytearray(store.get(output.content_hash))
    stored[-1] ^= 0x01
    store.force_put(output.content_hash, bytes(stored))

    verdict = verify_data_ops_receipt(run.receipt, store=store)
    assert not verdict.ok
    assert not verdict.evidence_reattached
    assert "reattaching" in verdict.reason


# ---------------------------------------------------------------------------
# AC5: determinism -- explicit row order, identical bytes and receipt hash
# ---------------------------------------------------------------------------


def test_row_order_is_canonical_across_runs(tmp_path: Path) -> None:
    # Two databases holding the same logical rows in different physical
    # (rowid) order: without ORDER BY the engine returns them differently, and
    # the driver's canonicaliser must erase that difference. ``id`` must NOT
    # alias the rowid here (no INTEGER PRIMARY KEY), so rowids -- and the full
    # scan order -- follow insertion order.
    ordered_ddl = (
        "CREATE TABLE t (id INTEGER, name TEXT NOT NULL)",
        "INSERT INTO t (id, name) VALUES (1, 'ada')",
        "INSERT INTO t (id, name) VALUES (2, 'bob')",
        "INSERT INTO t (id, name) VALUES (3, 'cyd')",
    )
    scrambled_ddl = (
        "CREATE TABLE t (id INTEGER, name TEXT NOT NULL)",
        "INSERT INTO t (id, name) VALUES (3, 'cyd')",
        "INSERT INTO t (id, name) VALUES (1, 'ada')",
        "INSERT INTO t (id, name) VALUES (2, 'bob')",
    )
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    _build_db(db_a, ordered_ddl)
    _build_db(db_b, scrambled_ddl)

    # Establish that the natural engine order genuinely differs between the
    # two fixtures -- otherwise this test would prove nothing.
    def natural_order(path: Path) -> list[tuple[int, str]]:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return list(conn.execute("SELECT id, name FROM t"))
        finally:
            conn.close()

    assert natural_order(db_a) != natural_order(db_b)

    keypair = generate_signer_keypair()
    run_a = _driver(SqliteQueryBackend(str(db_a)), tmp_path / "cas_a", keypair).run("SELECT id, name FROM t")
    run_b = _driver(SqliteQueryBackend(str(db_b)), tmp_path / "cas_b", keypair).run("SELECT id, name FROM t")

    assert run_a.canonical_result_bytes == run_b.canonical_result_bytes
    assert run_a.result_hash == run_b.result_hash
    assert run_a.result.rows == run_b.result.rows


def test_result_bytes_and_receipt_hash_identical_across_sdd_dirs(tmp_path: Path) -> None:
    # Same statement, same fixture DDL, same signing key, two entirely
    # separate .sdd roots: canonical result bytes must be byte-identical and
    # the receipt hash identical (deterministic Ed25519, deterministic plan).
    keypair = generate_signer_keypair()
    runs = []
    for name in ("one", "two"):
        sdd = tmp_path / name / ".sdd"
        db = tmp_path / name / "fixture.db"
        db.parent.mkdir(parents=True)
        _build_db(db)
        driver = _driver(SqliteQueryBackend(str(db)), sdd / "datasources" / "cas", keypair, connection_id="sales")
        runs.append(driver.run("SELECT id, name FROM t"))

    first, second = runs
    assert first.canonical_result_bytes == second.canonical_result_bytes
    assert first.receipt_hash == second.receipt_hash
    assert first.receipt_hash.startswith("sha256:")
    assert first.plan.plan_hash == second.plan.plan_hash


# ---------------------------------------------------------------------------
# AC6: schema drift is a typed, fail-closed verdict naming the objects
# ---------------------------------------------------------------------------


def test_schema_drift_is_typed_and_fail_closed(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in _FIXTURE_DDL:
        conn.execute(stmt)
    backend = SqliteQueryBackend(connection=conn)
    recorded = backend.schema_snapshot()

    # The schema moves under the recorded snapshot.
    conn.execute("ALTER TABLE t ADD COLUMN discount REAL")

    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    store_root = tmp_path / "cas"
    driver = _driver(backend, store_root)

    with pytest.raises(SchemaDrift) as excinfo:
        driver.run("SELECT id, name FROM t", expected_schema=recorded)

    drift = excinfo.value
    assert drift.recorded_digest == recorded.digest
    assert drift.live_digest != recorded.digest
    assert "t" in drift.changed_object_names
    assert any("discount" in d.detail for d in drift.drifts)
    # Fail closed: the submitted statement never executed (only the driver's
    # own schema introspection touched the connection) and nothing was signed.
    assert not any("FROM t" in s for s in traced)
    assert list(store_root.iterdir()) == []


def test_schema_mutation_between_snapshot_and_execute_refuses_to_sign(tmp_path: Path) -> None:
    # baz finding 3705959714: the snapshot and the execution may run on
    # separate connections, so the schema can move in the gap. The driver
    # re-snapshots after executing and must refuse to sign a result the
    # recorded snapshot no longer describes.
    conn = sqlite3.connect(":memory:")
    for stmt in _FIXTURE_DDL:
        conn.execute(stmt)

    class MutatingBackend:
        """Delegates to the real sqlite backend, but the schema moves mid-run."""

        name = "sqlite"

        def __init__(self) -> None:
            self._inner = SqliteQueryBackend(connection=conn)

        def schema_snapshot(self) -> object:
            return self._inner.schema_snapshot()

        def execute(self, sql: str, params: object = None, *, row_cap: int = 10_000) -> object:
            result = self._inner.execute(sql, params, row_cap=row_cap)  # type: ignore[arg-type]
            # The schema changes after the query ran but before the driver signs.
            conn.execute("ALTER TABLE t ADD COLUMN sneaky TEXT")
            return result

    store_root = tmp_path / "cas"
    driver = _driver(MutatingBackend(), store_root)  # type: ignore[arg-type]

    with pytest.raises(SchemaDrift) as excinfo:
        driver.run("SELECT id, name FROM t")

    drift = excinfo.value
    assert drift.recorded_digest != drift.live_digest
    assert "t" in drift.changed_object_names
    assert any("sneaky" in d.detail for d in drift.drifts)
    # Refused to sign: the store holds exactly the two signed input blobs
    # (schema + query) and no output blob, and no receipt was produced.
    assert len(list(store_root.iterdir())) == 2


# ---------------------------------------------------------------------------
# AC7: phase order is enforced by the machine the driver drives
# ---------------------------------------------------------------------------


def test_output_before_plan_raises_phase_error(tmp_path: Path) -> None:
    driver = _driver(SqliteQueryBackend(":memory:"), tmp_path / "cas")
    activity = driver.new_activity()
    activity.add_input(ref="schema:test", content=b"{}")
    with pytest.raises(DataOpsPhaseError):
        activity.add_output(ref="result:test", content=b"too early")


# ---------------------------------------------------------------------------
# Credentials never enter the recorded artifact
# ---------------------------------------------------------------------------


def test_connection_secret_never_recorded(tmp_path: Path) -> None:
    # The DSN (here: a filesystem path carrying a secret-looking component)
    # must appear in no receipt byte and no stored blob; provenance is the
    # connection id only.
    secret = "hunter2-topsecret-dsn-component"
    db_dir = tmp_path / secret
    db_dir.mkdir()
    db = db_dir / "fixture.db"
    _build_db(db)
    sdd = tmp_path / ".sdd"
    connection = DataSourceConnection(id="sales", driver="sqlite", dsn=str(db))

    driver = build_sqlite_query_driver(sdd, connection)
    run = driver.run("SELECT id, name FROM t")

    receipt_json = json.dumps(run.receipt.to_dict(), sort_keys=True)
    assert secret not in receipt_json
    assert "sales" in receipt_json  # provenance is the id, not the DSN
    cas_root = sdd / "datasources" / "cas"
    blobs = list(cas_root.iterdir())
    assert blobs, "expected signed input/output blobs in the content store"
    for blob in blobs:
        assert secret.encode("utf-8") not in blob.read_bytes()

    # The receipt still verifies offline from the same store.
    store = ContentStore(cas_root)
    assert verify_data_ops_receipt(run.receipt, store=store).ok
