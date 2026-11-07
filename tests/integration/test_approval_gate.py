"""Integration tests for the pre-spawn approval gate (#1110).

Covers the full sentinel/audit handshake exercised by
:func:`bernstein.core.orchestration.approval_gate.wait_for_approval`,
plus the CLI surface (``bernstein approve``/``reject``/``pending``).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from bernstein.core.models import ApprovalSpec
from click.testing import CliRunner

from bernstein.cli.commands.approve_cmd import approve
from bernstein.cli.commands.pending_cmd import pending
from bernstein.cli.commands.reject_cmd import reject
from bernstein.core.orchestration.approval_gate import (
    list_pending_approvals,
    wait_for_approval,
    write_pending_sentinel,
)
from bernstein.core.security.audit import AuditLog
from bernstein.core.tasks.lifecycle import set_audit_log


@pytest.fixture()
def audit_log(tmp_path: Path) -> Iterator[AuditLog]:
    """Wire a real :class:`AuditLog` into the lifecycle module for the test.

    Reset the global singleton afterwards so other integration tests do
    not observe leaked state.
    """
    log = AuditLog(tmp_path / "audit", key=b"k" * 32)
    set_audit_log(log)
    try:
        yield log
    finally:
        # Drop the global reference; downstream tests build their own.
        import bernstein.core.tasks.lifecycle as _lifecycle

        _lifecycle._audit_log = None


def _read_audit(audit_dir: Path) -> list[dict[str, object]]:
    """Return every audit row written under *audit_dir* in chain order."""
    entries: list[dict[str, object]] = []
    for log_file in sorted(audit_dir.glob("*.jsonl")):
        for raw in log_file.read_text().splitlines():
            if raw.strip():
                entries.append(json.loads(raw))
    return entries


def _operator_approves(workdir: Path, task_id: str) -> None:
    """Record a signed ``bernstein approve`` decision for the open request."""
    from bernstein.core.approval.models import local_shell_principal
    from bernstein.core.orchestration.approval_gate import record_decision

    record_decision(workdir, task_id, "approved", source="cli", principal=local_shell_principal().to_dict())


class TestWaitForApprovalSentinel:
    def test_sentinel_created_on_entry_then_cleared_on_resolve(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)

        # Land the operator's decision on the first poll so the loop exits
        # on the next iteration.
        approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"

        outcome = wait_for_approval(
            "T-1",
            spec,
            workdir=tmp_path,
            audit_log=audit_log,
            sleep=lambda _s: _operator_approves(tmp_path, "T-1"),
        )

        assert outcome == "approved"
        # Pending sentinel must be cleaned up so list_pending_approvals
        # does not surface a stale entry.
        assert not (approvals_dir / "T-1.pending").exists()

    def test_sentinel_payload_matches_spec(self, tmp_path: Path) -> None:
        spec = ApprovalSpec(prompt="ship?", timeout_seconds=42, default_action="approve")
        path = write_pending_sentinel(tmp_path, "T-2", spec, now=1_700_000_000.0)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["prompt"] == "ship?"
        assert payload["task_id"] == "T-2"
        assert payload["timeout_seconds"] == 42
        assert payload["default_action"] == "approve"
        assert payload["created_iso"].startswith("2023-")
        assert payload["timeout_at_iso"].startswith("2023-")


class TestApproveRejectCli:
    def test_approve_cli_unblocks_gate(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)
        outcomes: list[str] = []

        def _runner() -> None:
            outcomes.append(
                wait_for_approval(
                    "T-cli-approve",
                    spec,
                    workdir=tmp_path,
                    audit_log=audit_log,
                    poll_interval_s=0.05,
                )
            )

        thread = threading.Thread(target=_runner)
        thread.start()
        # Give the gate a moment to write its sentinel before we land
        # the decision.
        deadline = time.monotonic() + 2.0
        sentinel = tmp_path / ".sdd" / "runtime" / "approvals" / "T-cli-approve.pending"
        while time.monotonic() < deadline and not sentinel.exists():
            time.sleep(0.05)
        assert sentinel.exists(), "pending sentinel never written"

        runner = CliRunner()
        result = runner.invoke(
            approve,
            ["T-cli-approve", "--workdir", str(tmp_path), "--no-prompt"],
        )
        assert result.exit_code == 0, result.output

        thread.join(timeout=5)
        assert outcomes == ["approved"]

    def test_reject_cli_unblocks_gate(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)
        outcomes: list[str] = []

        def _runner() -> None:
            outcomes.append(
                wait_for_approval(
                    "T-cli-reject",
                    spec,
                    workdir=tmp_path,
                    audit_log=audit_log,
                    poll_interval_s=0.05,
                )
            )

        thread = threading.Thread(target=_runner)
        thread.start()
        deadline = time.monotonic() + 2.0
        sentinel = tmp_path / ".sdd" / "runtime" / "approvals" / "T-cli-reject.pending"
        while time.monotonic() < deadline and not sentinel.exists():
            time.sleep(0.05)
        assert sentinel.exists(), "pending sentinel never written"

        runner = CliRunner()
        result = runner.invoke(
            reject,
            ["T-cli-reject", "--workdir", str(tmp_path), "--no-prompt"],
        )
        assert result.exit_code == 0, result.output

        thread.join(timeout=5)
        assert outcomes == ["rejected"]

    def test_concurrent_approve_calls_first_wins(self, tmp_path: Path) -> None:
        runner = CliRunner()
        approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
        approvals_dir.mkdir(parents=True)

        first = runner.invoke(approve, ["T-race", "--workdir", str(tmp_path), "--no-prompt"])
        assert first.exit_code == 0
        assert "Approved" in first.output

        # Second invocation must report the existing decision rather than
        # silently rewriting state - that is what "first writer wins" means
        # at the operator-experience layer.
        second = runner.invoke(approve, ["T-race", "--workdir", str(tmp_path), "--no-prompt"])
        assert second.exit_code == 0
        assert "Already approved" in second.output

    def test_reject_after_approve_keeps_approval(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(approve, ["T-cross", "--workdir", str(tmp_path), "--no-prompt"])
        result = runner.invoke(reject, ["T-cross", "--workdir", str(tmp_path), "--no-prompt"])
        assert result.exit_code == 0
        assert "Already resolved" in result.output


class TestTimeoutBehaviour:
    def test_timeout_default_reject_resolves_to_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="?", timeout_seconds=1, default_action="reject")
        # Force the monotonic clock past the deadline immediately.
        clock = iter([0.0, 100.0, 100.0, 100.0])
        outcome = wait_for_approval(
            "T-timeout-reject",
            spec,
            workdir=tmp_path,
            audit_log=audit_log,
            monotonic=lambda: next(clock),
            sleep=lambda _s: None,
        )
        assert outcome == "timeout"
        # On reject-style timeout the gate persists a .rejected file so
        # downstream readers see a terminal state.
        assert (tmp_path / ".sdd" / "runtime" / "approvals" / "T-timeout-reject.rejected").exists()

    def test_timeout_default_approve_resolves_to_approved(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="?", timeout_seconds=1, default_action="approve")
        clock = iter([0.0, 100.0, 100.0, 100.0])
        outcome = wait_for_approval(
            "T-timeout-approve",
            spec,
            workdir=tmp_path,
            audit_log=audit_log,
            monotonic=lambda: next(clock),
            sleep=lambda _s: None,
        )
        assert outcome == "timeout"
        # default_action=approve persists an .approved file so the body
        # would be allowed to run on resume.
        assert (tmp_path / ".sdd" / "runtime" / "approvals" / "T-timeout-approve.approved").exists()


class TestAuditChain:
    def test_pending_then_resolved_events_in_order(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)
        # Land a signed CLI decision on the first poll.
        outcome = wait_for_approval(
            "T-audit",
            spec,
            workdir=tmp_path,
            audit_log=audit_log,
            sleep=lambda _s: _operator_approves(tmp_path, "T-audit"),
        )
        assert outcome == "approved"

        events = _read_audit(tmp_path / "audit")
        approval_events = [e for e in events if str(e.get("event_type", "")).startswith("approval_")]
        assert [e["event_type"] for e in approval_events] == [
            "approval_pending",
            "approval_resolved",
        ]
        # Both events must reference the gated task and the chain must verify.
        assert all(e["resource_id"] == "T-audit" for e in approval_events)
        valid, errors = audit_log.verify()
        assert valid, errors

    def test_timeout_emits_timeout_default_decision_source(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="?", timeout_seconds=1, default_action="reject")
        clock = iter([0.0, 100.0, 100.0])
        outcome = wait_for_approval(
            "T-audit-timeout",
            spec,
            workdir=tmp_path,
            audit_log=audit_log,
            monotonic=lambda: next(clock),
            sleep=lambda _s: None,
        )
        assert outcome == "timeout"

        events = _read_audit(tmp_path / "audit")
        resolved = [e for e in events if e.get("event_type") == "approval_resolved"]
        assert resolved, "no approval_resolved event"
        details = resolved[-1]["details"]
        assert isinstance(details, dict)
        assert details["decision_source"] == "timeout-default"
        assert details["outcome"] == "timeout"
        assert details["default_action"] == "reject"


class TestPendingCommand:
    def test_pending_lists_approval_sentinels(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="ship A?", timeout_seconds=600)
        write_pending_sentinel(tmp_path, "T-A", spec)
        spec_b = ApprovalSpec(prompt="ship B?", timeout_seconds=600, default_action="approve")
        write_pending_sentinel(tmp_path, "T-B", spec_b)

        rows = list_pending_approvals(tmp_path)
        ids = {row.get("task_id") for row in rows}
        assert ids == {"T-A", "T-B"}

        runner = CliRunner()
        result = runner.invoke(pending, ["--workdir", str(tmp_path), "--kind", "approval"])
        assert result.exit_code == 0, result.output
        assert "T-A" in result.output
        assert "T-B" in result.output

    def test_pending_distinguishes_approval_from_spawn(self, tmp_path: Path) -> None:
        # Create a legacy spawn-pending entry alongside an approval-pending one.
        approval_dir = tmp_path / ".sdd" / "runtime" / "approvals"
        approval_dir.mkdir(parents=True)
        write_pending_sentinel(tmp_path, "T-A", ApprovalSpec(prompt="approval gate"))

        spawn_dir = tmp_path / ".sdd" / "runtime" / "pending_approvals"
        spawn_dir.mkdir(parents=True)
        (spawn_dir / "T-S.json").write_text(
            json.dumps({"task_id": "T-S", "task_title": "spawn ready", "test_summary": "ok"})
        )

        runner = CliRunner()
        result = runner.invoke(pending, ["--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "Approval-pending" in result.output
        assert "Spawn-pending" in result.output
        assert "T-A" in result.output
        assert "T-S" in result.output
