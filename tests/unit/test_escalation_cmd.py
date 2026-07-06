"""CLI tests for ``bernstein escalation show|verify`` (#2299).

``verify`` reconstructs the trailing failure window from the run journal and
confirms it matches the receipt; a tampered journal entry inside the window
fails offline. The receipt is assembled directly via the core API so the test
stays offline and does not depend on a live stall.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.escalation_cmd import escalation_group
from bernstein.core.orchestration.escalation import (
    assemble_escalation_receipt,
    load_or_create_escalation_identity,
)
from bernstein.core.orchestration.supervisor_receipt import StallReason
from bernstein.core.replay.journal import EventJournal

_HMAC_KEY = b"k" * 32
_RUN_ID = "run-cli-1"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Scope the audit HMAC key to tmp_path so the CLI resolves the same key the
    # fixture signed with, and no state leaks across tests.
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_receipt(project: Path) -> str:
    sdd = project / ".sdd"
    journal = EventJournal(_RUN_ID, sdd)
    for i in range(12):
        if i == 3:
            journal.record("snapshot", snapshot_sha="d" * 40, step_index=i)
        else:
            journal.record("task.tick", session_id="sess-1", seq=i)

    from bernstein.core.security.audit import load_or_create_audit_key

    hmac_key = load_or_create_audit_key()
    private_pem, public_pem = load_or_create_escalation_identity(sdd / "identity")
    receipt = assemble_escalation_receipt(
        sdd_dir=sdd,
        lineage_root=sdd / "lineage",
        hmac_key=hmac_key,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        run_id=_RUN_ID,
        worker_id="abcdef012345",
        session_id="sess-1",
        worktree_id="wt-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        respawn_budget_remaining=2,
        fork_step=3,
        install_rev="abc1234567890def",
        timestamp=1_700_000_000,
    )
    return receipt.receipt_id


def test_show_then_verify_ok(project: Path) -> None:
    receipt_id = _make_receipt(project)
    runner = CliRunner()
    show = runner.invoke(escalation_group, ["show", receipt_id, "-w", str(project)])
    assert show.exit_code == 0, show.output
    assert "recommended_action" in show.output
    assert "resume fork" in show.output

    verify = runner.invoke(escalation_group, ["verify", receipt_id, "-w", str(project)])
    assert verify.exit_code == 0, verify.output
    assert "OK" in verify.output


def test_verify_tampered_journal_exit_2(project: Path) -> None:
    import json

    receipt_id = _make_receipt(project)
    journal_path = project / ".sdd" / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["seq"] = 9999
    lines[-1] = json.dumps(last)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(escalation_group, ["verify", receipt_id, "-w", str(project)])
    assert result.exit_code == 2, result.output
    assert "MISMATCH" in result.output


def test_show_no_receipt_exit_1(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(escalation_group, ["show", "0" * 32, "-w", str(project)])
    assert result.exit_code == 1, result.output
    assert "NO RECEIPT" in result.output
