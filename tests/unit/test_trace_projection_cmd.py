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
from bernstein.core.replay.journal import EventJournal

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
