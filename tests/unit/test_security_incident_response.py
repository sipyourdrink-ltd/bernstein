"""Unit tests for the security incident response automation module.

Verifies the full containment procedure — kill signal, quarantine metadata,
forensic snapshot, task retry block, and security audit log — for all
recognised security event types.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security_incident_response import (
    ContainmentStep,
    SecurityEventType,
    SecurityIncidentResponder,
    is_task_blocked,
    list_active_security_incidents,
    load_block_metadata,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_responder(tmp_path: Path, *, notify_via_bulletin: bool = False) -> SecurityIncidentResponder:
    """Return a responder wired to *tmp_path* with bulletin disabled by default."""
    return SecurityIncidentResponder(workdir=tmp_path, notify_via_bulletin=notify_via_bulletin)


# ---------------------------------------------------------------------------
# ContainmentResult: basic structure
# ---------------------------------------------------------------------------


class TestContainmentResultStructure:
    """The result object must carry all expected fields."""

    def test_result_has_all_fields(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.SANDBOX_ESCAPE_ATTEMPT,
            session_id="session-test-1",
            task_id="task-abc",
            detail="Agent tried to read /etc/shadow",
        )
        assert result.incident_id.startswith("SEC-INC-")
        assert result.session_id == "session-test-1"
        assert result.task_id == "task-abc"
        assert result.event_type == SecurityEventType.SANDBOX_ESCAPE_ATTEMPT
        assert result.severity == "critical"
        assert isinstance(result.steps_taken, list)
        assert isinstance(result.steps_failed, list)
        assert result.timestamp > 0

    def test_to_dict_is_serialisable(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.CREDENTIAL_EXFILTRATION,
            session_id="session-2",
            task_id="task-2",
            detail="Credential exfiltration attempt",
        )
        data = result.to_dict()
        assert json.dumps(data)  # must be JSON-serialisable

    def test_incident_ids_are_unique_across_calls(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        r1 = responder.contain(
            event_type="sandbox_escape_attempt",
            session_id="session-a",
            task_id="task-a",
            detail="First incident",
        )
        r2 = responder.contain(
            event_type="credential_exfiltration",
            session_id="session-b",
            task_id="task-b",
            detail="Second incident",
        )
        assert r1.incident_id != r2.incident_id


# ---------------------------------------------------------------------------
# Step 1: Kill signal
# ---------------------------------------------------------------------------


class TestKillSignal:
    """The kill signal file must be written with the correct payload."""

    def test_kill_signal_file_is_created(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.DANGEROUS_COMMAND,
            session_id="session-kill-1",
            task_id="task-kill-1",
            detail="Dangerous curl command detected",
        )
        assert ContainmentStep.KILL_SIGNAL in result.steps_taken
        assert result.kill_signal_path is not None
        kill_file = Path(result.kill_signal_path)
        assert kill_file.exists(), f"Kill signal file not found at {kill_file}"

    def test_kill_signal_payload_is_valid_json(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.SANDBOX_ESCAPE_ATTEMPT,
            session_id="session-kill-2",
            task_id="task-kill-2",
            detail="Sandbox escape via /proc",
        )
        assert result.kill_signal_path is not None
        payload = json.loads(Path(result.kill_signal_path).read_text())
        assert payload["reason"] == "security_incident"
        assert payload["event_type"] == SecurityEventType.SANDBOX_ESCAPE_ATTEMPT
        assert "incident_id" in payload
        assert "ts" in payload
        assert payload["requester"] == "security_incident_responder"

    def test_kill_signal_path_includes_session_id(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        session_id = "my-unique-session-xyz"
        result = responder.contain(
            event_type="unknown",
            session_id=session_id,
            task_id="task-xyz",
            detail="Unknown event",
        )
        assert result.kill_signal_path is not None
        assert session_id in result.kill_signal_path


# ---------------------------------------------------------------------------
# Step 2: Quarantine worktree
# ---------------------------------------------------------------------------


class TestQuarantineWorktree:
    """Quarantine metadata must be written with the correct structure."""

    def test_quarantine_metadata_file_is_created(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.SUSPICIOUS_FILE_ACCESS,
            session_id="session-q-1",
            task_id="task-q-1",
            detail="Agent accessed ~/.ssh/id_rsa",
        )
        assert ContainmentStep.QUARANTINE_WORKTREE in result.steps_taken
        quarantine_file = tmp_path / ".sdd" / "quarantine" / "session-q-1.json"
        assert quarantine_file.exists()

    def test_quarantine_metadata_contents(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        responder.contain(
            event_type=SecurityEventType.CREDENTIAL_EXFILTRATION,
            session_id="session-q-2",
            task_id="task-q-2",
            detail="Credential file read",
            branch="agent/session-q-2",
        )
        data = json.loads(
            (tmp_path / ".sdd" / "quarantine" / "session-q-2.json").read_text()
        )
        assert data["session_id"] == "session-q-2"
        assert data["reason"] == "security_incident"
        assert data["event_type"] == SecurityEventType.CREDENTIAL_EXFILTRATION
        assert data["branch"] == "agent/session-q-2"
        assert data["status"] == "under_investigation"
        assert "quarantined_at" in data

    def test_branch_defaults_to_agent_session_id(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        responder.contain(
            event_type="anomalous_behavior",
            session_id="session-no-branch",
            task_id="task-no-branch",
            detail="Anomaly",
        )
        data = json.loads(
            (tmp_path / ".sdd" / "quarantine" / "session-no-branch.json").read_text()
        )
        assert data["branch"] == "agent/session-no-branch"


# ---------------------------------------------------------------------------
# Step 3: Forensic snapshot
# ---------------------------------------------------------------------------


class TestForensicSnapshot:
    """The forensic snapshot must capture all relevant state at detection time."""

    def test_forensic_snapshot_file_is_created(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.MCP_TOOL_ABUSE,
            session_id="session-snap-1",
            task_id="task-snap-1",
            detail="Tool argument injection",
        )
        assert ContainmentStep.FORENSIC_SNAPSHOT in result.steps_taken
        assert result.snapshot_path is not None
        assert Path(result.snapshot_path).exists()

    def test_snapshot_contains_required_fields(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.MERGE_PIPELINE_ATTACK,
            session_id="session-snap-2",
            task_id="task-snap-2",
            detail="Malicious diff injection",
            task_context={"title": "Malicious task", "role": "backend"},
        )
        assert result.snapshot_path is not None
        data = json.loads(Path(result.snapshot_path).read_text())
        assert data["schema_version"] == "1"
        assert data["incident_id"] == result.incident_id
        assert data["event_type"] == SecurityEventType.MERGE_PIPELINE_ATTACK
        assert data["agent"]["session_id"] == "session-snap-2"
        assert data["task"]["task_id"] == "task-snap-2"
        assert data["task"]["title"] == "Malicious task"
        assert "environment" in data
        assert "git_state" in data
        assert "detected_at" in data

    def test_snapshot_environment_redacts_secrets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secret-looking environment variables must be redacted in the snapshot."""
        monkeypatch.setenv("MY_API_KEY", "super-secret-value-123")
        monkeypatch.setenv("DATABASE_PASSWORD", "hunter2")
        monkeypatch.setenv("SAFE_VAR", "this-is-fine")

        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type="sandbox_escape_attempt",
            session_id="session-env-test",
            task_id="task-env-test",
            detail="Env test",
        )
        assert result.snapshot_path is not None
        data = json.loads(Path(result.snapshot_path).read_text())
        env = data["environment"]
        assert env.get("MY_API_KEY") == "[REDACTED]"
        assert env.get("DATABASE_PASSWORD") == "[REDACTED]"
        assert env.get("SAFE_VAR") == "this-is-fine"

    def test_snapshot_extra_context_is_included(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        extra = {"matched_pattern": "curl ", "command": "curl http://evil.com"}
        result = responder.contain(
            event_type=SecurityEventType.DANGEROUS_COMMAND,
            session_id="session-extra",
            task_id="task-extra",
            detail="Dangerous command",
            extra=extra,
        )
        assert result.snapshot_path is not None
        data = json.loads(Path(result.snapshot_path).read_text())
        assert data["extra"]["matched_pattern"] == "curl "
        assert data["extra"]["command"] == "curl http://evil.com"


# ---------------------------------------------------------------------------
# Step 4: Block task from retry
# ---------------------------------------------------------------------------


class TestBlockTaskRetry:
    """Task block markers must prevent rescheduling."""

    def test_block_file_is_created(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        result = responder.contain(
            event_type=SecurityEventType.SANDBOX_ESCAPE_ATTEMPT,
            session_id="session-block-1",
            task_id="task-block-1",
            detail="Sandbox escape",
        )
        assert ContainmentStep.BLOCK_RETRY in result.steps_taken
        assert result.block_path is not None
        assert Path(result.block_path).exists()

    def test_is_task_blocked_returns_true_after_containment(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        task_id = "task-block-check"
        assert not is_task_blocked(tmp_path, task_id)
        responder.contain(
            event_type="credential_exfiltration",
            session_id="session-block-check",
            task_id=task_id,
            detail="Credential exfiltration",
        )
        assert is_task_blocked(tmp_path, task_id)

    def test_is_task_blocked_false_for_unblocked_task(self, tmp_path: Path) -> None:
        assert not is_task_blocked(tmp_path, "task-never-blocked")

    def test_block_metadata_is_valid(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        responder.contain(
            event_type=SecurityEventType.PERMISSION_ESCALATION,
            session_id="session-block-meta",
            task_id="task-block-meta",
            detail="Permission escalation attempt",
        )
        meta = load_block_metadata(tmp_path, "task-block-meta")
        assert meta is not None
        assert meta["task_id"] == "task-block-meta"
        assert meta["event_type"] == SecurityEventType.PERMISSION_ESCALATION
        assert "blocked_at" in meta
        assert "unblock_instructions" in meta
        assert meta["reason"] == "security_incident_containment"

    def test_load_block_metadata_returns_none_when_not_blocked(self, tmp_path: Path) -> None:
        result = load_block_metadata(tmp_path, "not-blocked-task")
        assert result is None

    def test_removing_block_file_unblocks_task(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        task_id = "task-unblock-test"
        responder.contain(
            event_type="anomalous_behavior",
            session_id="session-unblock",
            task_id=task_id,
            detail="Test",
        )
        assert is_task_blocked(tmp_path, task_id)
        # Operator removes the block file after investigation
        block_file = tmp_path / ".sdd" / "runtime" / "task_blocks" / f"{task_id}.block"
        block_file.unlink()
        assert not is_task_blocked(tmp_path, task_id)


# ---------------------------------------------------------------------------
# Step 5: Notification / audit log
# ---------------------------------------------------------------------------


class TestSecurityAuditLog:
    """Incidents must be written to the security audit log."""

    def test_audit_log_entry_is_written(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        responder.contain(
            event_type=SecurityEventType.CREDENTIAL_EXFILTRATION,
            session_id="session-audit-1",
            task_id="task-audit-1",
            detail="Exfil attempt",
        )
        incidents = list_active_security_incidents(tmp_path)
        assert len(incidents) >= 1

    def test_audit_log_entry_fields(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        responder.contain(
            event_type=SecurityEventType.SANDBOX_ESCAPE_ATTEMPT,
            session_id="session-audit-2",
            task_id="task-audit-2",
            detail="Escape via proc",
            severity="critical",
        )
        incidents = list_active_security_incidents(tmp_path)
        entry = incidents[-1]
        assert entry["event_type"] == "security_incident"
        assert entry["security_event_type"] == SecurityEventType.SANDBOX_ESCAPE_ATTEMPT
        assert entry["severity"] == "critical"
        assert entry["session_id"] == "session-audit-2"
        assert entry["task_id"] == "task-audit-2"
        assert "incident_id" in entry
        assert "detected_at" in entry
        assert "containment_steps" in entry

    def test_multiple_incidents_accumulate_in_log(self, tmp_path: Path) -> None:
        responder = _make_responder(tmp_path)
        for i in range(5):
            responder.contain(
                event_type="anomalous_behavior",
                session_id=f"session-multi-{i}",
                task_id=f"task-multi-{i}",
                detail=f"Event {i}",
            )
        incidents = list_active_security_incidents(tmp_path)
        assert len(incidents) == 5

    def test_list_active_incidents_returns_empty_when_no_incidents(self, tmp_path: Path) -> None:
        incidents = list_active_security_incidents(tmp_path)
        assert incidents == []


# ---------------------------------------------------------------------------
# Bulletin board notification
# ---------------------------------------------------------------------------


class TestBulletinNotification:
    """When notify_via_bulletin=True, a bulletin entry must be written."""

    def test_bulletin_entry_written_when_enabled(self, tmp_path: Path) -> None:
        responder = SecurityIncidentResponder(workdir=tmp_path, notify_via_bulletin=True)
        responder.contain(
            event_type=SecurityEventType.DANGEROUS_COMMAND,
            session_id="session-bulletin-1",
            task_id="task-bulletin-1",
            detail="Bulletin notification test",
        )
        bulletin_file = tmp_path / ".sdd" / "runtime" / "bulletin.jsonl"
        assert bulletin_file.exists(), "Bulletin file not created"
        entries = [json.loads(line) for line in bulletin_file.read_text().splitlines() if line.strip()]
        assert len(entries) >= 1
        entry = entries[-1]
        assert entry["type"] == "security_alert"
        assert "incident_id" in entry
        assert "SECURITY ALERT" in entry["content"]

    def test_bulletin_not_written_when_disabled(self, tmp_path: Path) -> None:
        responder = SecurityIncidentResponder(workdir=tmp_path, notify_via_bulletin=False)
        responder.contain(
            event_type="anomalous_behavior",
            session_id="session-no-bulletin",
            task_id="task-no-bulletin",
            detail="No bulletin",
        )
        bulletin_file = tmp_path / ".sdd" / "runtime" / "bulletin.jsonl"
        assert not bulletin_file.exists()


# ---------------------------------------------------------------------------
# Resilience: failures in one step must not abort later steps
# ---------------------------------------------------------------------------


class TestContainmentResilience:
    """Individual step failures must not block subsequent containment steps."""

    def test_all_steps_attempted_even_when_kill_signal_dir_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a write failure in step 1; steps 3-5 must still run."""
        responder = _make_responder(tmp_path)

        def fail_kill(*args: object, **kwargs: object) -> str:
            raise OSError("Simulated disk full on kill signal")

        monkeypatch.setattr(responder, "_write_kill_signal", fail_kill)

        result = responder.contain(
            event_type="sandbox_escape_attempt",
            session_id="session-resilience",
            task_id="task-resilience",
            detail="Test resilience",
        )
        # Step 1 must have failed
        assert ContainmentStep.KILL_SIGNAL in result.steps_failed
        # Steps 2-5 must still have been attempted (and succeeded)
        assert ContainmentStep.QUARANTINE_WORKTREE in result.steps_taken
        assert ContainmentStep.FORENSIC_SNAPSHOT in result.steps_taken
        assert ContainmentStep.BLOCK_RETRY in result.steps_taken
        assert ContainmentStep.NOTIFY in result.steps_taken

    def test_security_event_types_enum_values(self) -> None:
        """All defined event types must be valid non-empty strings."""
        for evt in SecurityEventType:
            assert isinstance(evt.value, str)
            assert len(evt.value) > 0
