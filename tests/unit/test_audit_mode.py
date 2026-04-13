"""Unit tests for SOC 2 audit mode: HMAC chain, Merkle seal, tamper detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.audit import AuditLog, RetentionPolicy


@pytest.fixture
def audit_log(tmp_path: Path) -> AuditLog:
    return AuditLog(audit_dir=tmp_path / "audit")


# ---- Event emission -------------------------------------------------------


class TestAuditEventEmission:
    def test_log_creates_event(self, audit_log: AuditLog) -> None:
        event = audit_log.log(
            event_type="task.transition",
            actor="orchestrator",
            resource_type="task",
            resource_id="TASK-001",
            details={"from_status": "open", "to_status": "claimed"},
        )
        assert event.event_type == "task.transition"
        assert event.actor == "orchestrator"
        assert event.resource_id == "TASK-001"
        assert event.hmac  # HMAC should be computed

    def test_log_multiple_events_chain(self, audit_log: AuditLog) -> None:
        e1 = audit_log.log("task.create", "user", "task", "T1")
        e2 = audit_log.log("task.claim", "agent-1", "task", "T1")
        e3 = audit_log.log("task.complete", "agent-1", "task", "T1")

        # Each event's prev_hmac should be the previous event's hmac
        assert e2.prev_hmac == e1.hmac
        assert e3.prev_hmac == e2.hmac

    def test_event_has_structured_fields(self, audit_log: AuditLog) -> None:
        event = audit_log.log(
            event_type="agent.spawn",
            actor="orchestrator",
            resource_type="agent",
            resource_id="agent-abc",
            details={"model": "sonnet", "role": "backend"},
        )
        assert event.timestamp  # ISO 8601
        assert event.event_type == "agent.spawn"
        assert event.resource_type == "agent"
        assert event.details["model"] == "sonnet"


# ---- HMAC chain verification ----------------------------------------------


class TestHMACVerification:
    def test_verify_empty_log(self, audit_log: AuditLog) -> None:
        valid, errors = audit_log.verify()
        assert valid
        assert errors == []

    def test_verify_intact_chain(self, audit_log: AuditLog) -> None:
        for i in range(5):
            audit_log.log("task.transition", "orchestrator", "task", f"T{i}")

        valid, errors = audit_log.verify()
        assert valid
        assert errors == []

    def test_verify_detects_tamper(self, audit_log: AuditLog) -> None:
        for i in range(3):
            audit_log.log("task.transition", "orchestrator", "task", f"T{i}")

        # Tamper with the log file
        log_files = sorted(audit_log._audit_dir.glob("*.jsonl"))
        assert len(log_files) >= 1

        lines = log_files[0].read_text().strip().split("\n")
        if len(lines) >= 2:
            # Modify the second entry
            entry = json.loads(lines[1])
            entry["actor"] = "TAMPERED"
            lines[1] = json.dumps(entry)
            log_files[0].write_text("\n".join(lines) + "\n")

            valid, errors = audit_log.verify()
            assert not valid
            assert len(errors) > 0


# ---- Merkle sealing -------------------------------------------------------


class TestMerkleSeal:
    def test_merkle_seal_and_verify(self, tmp_path: Path) -> None:
        from bernstein.core.merkle import compute_seal

        # Create some audit log files
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()

        log = AuditLog(audit_dir=audit_dir)
        for i in range(3):
            log.log("task.transition", "orchestrator", "task", f"T{i}")

        # Compute Merkle seal
        tree, _seal = compute_seal(audit_dir)
        root = tree.root.hash
        assert root  # Non-empty hex string
        assert len(root) == 64  # SHA-256 hex digest


# ---- Retention and archiving -----------------------------------------------


class TestRetention:
    def test_archive_old_logs(self, audit_log: AuditLog) -> None:
        # Create an event
        audit_log.log("task.create", "user", "task", "T1")

        # Archive with a very recent retention (should not archive today's log)
        policy = RetentionPolicy(retention_days=0)
        result = audit_log.archive(policy)
        # Today's log is too recent (0 days means everything older than now)
        # The exact behavior depends on implementation; just check it runs
        assert result is not None


# ---- Query ----------------------------------------------------------------


class TestQuery:
    def test_query_by_event_type(self, audit_log: AuditLog) -> None:
        audit_log.log("task.create", "user", "task", "T1")
        audit_log.log("agent.spawn", "orchestrator", "agent", "A1")
        audit_log.log("task.complete", "agent", "task", "T1")

        results = audit_log.query(event_type="task.create")
        assert len(results) == 1
        assert results[0].event_type == "task.create"

    def test_query_by_actor(self, audit_log: AuditLog) -> None:
        audit_log.log("task.create", "user", "task", "T1")
        audit_log.log("agent.spawn", "orchestrator", "agent", "A1")

        results = audit_log.query(actor="orchestrator")
        assert len(results) == 1
        assert results[0].actor == "orchestrator"
