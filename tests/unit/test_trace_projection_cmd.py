"""CLI tests for ``bernstein trace project|verify-projection`` (#2300).

The projection is emitted from a run's event journal and verified back
against it. The install signing key and the audit key are isolated per
test via tmp_path + monkeypatch so nothing bleeds across runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd
from bernstein.core.replay.journal import EventJournal, run_journal_path
from bernstein.core.security.audit import AuditLog, RetentionPolicy, load_audit_key
from bernstein.core.security.audit_chain import (
    EVENT_OTEL_PROJECTION,
    AuditChainStore,
    record_otel_projection,
)

_RUN_ID = "run-1"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a recorded run journal and isolated keys."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.setenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", str(tmp_path / "install.key"))

    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")
    journal.record("run_started", goal="ship")
    journal.record("agent_spawned", agent_id="a1")
    journal.record("task_claimed", task_id="t1")
    journal.record("task_completed", task_id="t1")
    journal.record("agent_reaped", agent_id="a1")
    journal.record("run_completed", ok=True)
    return tmp_path


def _audit_store(project: Path) -> AuditChainStore:
    """Open the fixture's audit chain with its existing key."""
    return AuditChainStore(project / ".sdd" / "audit", key=load_audit_key())


def _replace_projection_audit(project: Path, *, field: str, value: object) -> None:
    """Replace the genuine row with one authenticated mismatched field."""
    audit_dir = project / ".sdd" / "audit"
    rows = _audit_store(project).query(event_type=EVENT_OTEL_PROJECTION)
    assert len(rows) == 1
    details = dict(rows[0].details)
    details[field] = value

    for log in audit_dir.glob("*.jsonl"):
        log.unlink()
    record_otel_projection(
        chain=_audit_store(project),
        run_id=str(details["run_id"]),
        journal_head=str(details["journal_head"]),
        trace_id=str(details["trace_id"]),
        span_count=int(details["span_count"]),
        projection_sha256=str(details["projection_sha256"]),
    )


def test_project_writes_signed_span_set(project: Path) -> None:
    """AC5: with no OTLP endpoint set, the local projection still emits."""
    runner = CliRunner()
    result = runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    dest = project / ".sdd" / "runs" / _RUN_ID / "projection.otel.json"
    assert dest.exists()
    payload = json.loads(dest.read_text())
    assert payload["signature_b64"]
    assert len(payload["trace_id"]) == 32
    assert payload["spans"]


def test_project_records_audit_event(project: Path) -> None:
    """The emit binds the span set to the journal via an audit-chain event."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    chain = AuditChainStore(project / ".sdd" / "audit", key=load_or_create_audit_key(project / "audit.key"))
    events = chain.query(event_type=EVENT_OTEL_PROJECTION)
    assert events
    assert events[0].details["run_id"] == _RUN_ID


def test_verify_projection_ok(project: Path) -> None:
    """AC3: verify-projection recomputes span ids and confirms the signature."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_projection_rejects_tampered_span(project: Path) -> None:
    """AC3: an altered span id fails verification with exit code 2."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    dest = project / ".sdd" / "runs" / _RUN_ID / "projection.otel.json"
    payload = json.loads(dest.read_text())
    payload["spans"][1]["span_id"] = "deadbeefdeadbeef"
    dest.write_text(json.dumps(payload))
    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 2, result.output
    assert "VERIFICATION FAILED" in result.output


def test_project_missing_journal_errors(project: Path) -> None:
    """A run with no journal is an input error, not a fabricated span set."""
    runner = CliRunner()
    result = runner.invoke(trace_cmd, ["project", "no-such-run", "--workdir", str(project)])
    assert result.exit_code != 0
    assert "no event journal" in result.output


def test_two_projections_byte_identical(project: Path) -> None:
    """AC1: re-emitting the same journal yields byte-identical bytes."""
    runner = CliRunner()
    first = runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project), "--json"])
    second = runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project), "--json"])
    assert first.exit_code == 0
    assert first.output == second.output


def test_verify_projection_corrupted_journal_exits_one(project: Path) -> None:
    """A journal that does not fully parse must fail the verifier (#3549).

    Same contract as the sibling ``telemetry verify-span`` test: exit 1
    (bad input) naming the physical line, never a pass over a filtered view.
    """
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])

    journal_path = run_journal_path(project / ".sdd", _RUN_ID)
    with journal_path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 1, result.output
    assert "corrupted" in result.output.lower()
    assert "physical line" in result.output.lower()


def test_verify_projection_unanchored_is_unverifiable_when_chain_row_missing(project: Path) -> None:
    """A valid projection that has no otel.projection evidence is unverifiable (exit 1)."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])

    audit_dir = project / ".sdd" / "audit"
    for log in sorted(audit_dir.glob("*.jsonl")):
        log.unlink()

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 1, result.output
    assert "UNVERIFIABLE" in result.output.upper()


def test_verify_projection_missing_audit_key_is_unverifiable_and_read_only(project: Path) -> None:
    """A verifier never mints a replacement key for evidence it cannot authenticate."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    key_path = project / "audit.key"
    key_path.unlink()

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 1, result.output
    assert "audit key" in result.output.lower()
    assert not key_path.exists()


def test_verify_projection_missing_audit_directory_is_not_recreated(project: Path) -> None:
    """Missing evidence stays missing after the read-only verification path."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    audit_dir = project / ".sdd" / "audit"
    for item in audit_dir.iterdir():
        item.unlink()
    audit_dir.rmdir()

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 1, result.output
    assert "audit directory" in result.output.lower()
    assert not audit_dir.exists()


def test_verify_projection_ignores_projection_event_for_another_run(project: Path) -> None:
    """An authenticated row for another run is not evidence for the requested run."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    _replace_projection_audit(project, field="run_id", value="another-run")

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 1, result.output
    assert "UNVERIFIABLE" in result.output.upper()
    assert "no otel.projection audit event" in result.output.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("journal_head", "0" * 64),
        ("trace_id", "0" * 32),
        ("span_count", 999),
        ("projection_sha256", "0" * 64),
    ],
)
def test_verify_projection_rejects_authenticated_audit_binding_mismatch(
    project: Path,
    field: str,
    value: object,
) -> None:
    """Authenticated evidence for the run must match every bound field."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    _replace_projection_audit(project, field=field, value=value)

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 2, result.output
    assert "VERIFICATION FAILED" in result.output.upper()
    assert field in result.output


def test_verify_projection_accepts_any_exact_event_for_repeated_projection(project: Path) -> None:
    """A stale sibling row does not mask an exact event for a repeated projection."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    genuine = _audit_store(project).query(event_type=EVENT_OTEL_PROJECTION)[0].details
    record_otel_projection(
        chain=_audit_store(project),
        run_id=_RUN_ID,
        journal_head=str(genuine["journal_head"]),
        trace_id=str(genuine["trace_id"]),
        span_count=int(genuine["span_count"]),
        projection_sha256="0" * 64,
    )

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_projection_accepts_archived_audit_evidence(project: Path) -> None:
    """Retention must not orphan a projection whose evidence moved to the archive."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    audit_dir = project / ".sdd" / "audit"
    archived = AuditLog(audit_dir, key=load_audit_key()).archive(RetentionPolicy(retention_days=-1))
    assert archived.archived
    assert not list(audit_dir.glob("*.jsonl"))

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_projection_corrupt_audit_chain_is_unverifiable(project: Path) -> None:
    """Unauthenticated rows cannot support either a genuine or forged verdict."""
    runner = CliRunner()
    runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    log_path = next((project / ".sdd" / "audit").glob("*.jsonl"))
    row = json.loads(log_path.read_text(encoding="utf-8"))
    row["details"]["projection_sha256"] = "0" * 64
    log_path.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    result = runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert result.exit_code == 1, result.output
    assert "UNVERIFIABLE" in result.output.upper()
    assert "integrity" in result.output.lower()
