import json
from pathlib import Path

from bernstein.core.audit import _GENESIS_HMAC, AuditLog  # pyright: ignore[reportPrivateUsage]


def test_audit_log_record(tmp_path: Path) -> None:
    """Test recording a single audit event."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")

    event = log.log("task.created", "system", "task", "task-1", {"foo": "bar"})

    assert event.event_type == "task.created"
    assert event.actor == "system"
    assert event.resource_id == "task-1"
    assert event.details == {"foo": "bar"}
    assert event.prev_hmac == _GENESIS_HMAC
    assert len(event.hmac) == 64


def test_audit_log_persistence(tmp_path: Path) -> None:
    """Test that audit log state is persisted and recoverable."""
    audit_dir = tmp_path / "audit"
    key = b"test-key"
    log1 = AuditLog(audit_dir, key=key)
    event1 = log1.log("type1", "actor1", "res", "id1")

    # Reload log from same directory with same key
    log2 = AuditLog(audit_dir, key=key)
    assert log2._prev_hmac == event1.hmac  # pyright: ignore[reportPrivateUsage]

    events = log2.query()
    assert len(events) == 1
    assert events[0].event_type == "type1"
    assert events[0].hmac == event1.hmac


def test_audit_log_chaining(tmp_path: Path) -> None:
    """Test that events are chained via HMAC."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")

    event1 = log.log("e1", "a1", "r1", "i1")
    event2 = log.log("e2", "a2", "r2", "i2")

    assert event2.prev_hmac == event1.hmac
    assert event2.hmac != event1.hmac


def test_audit_log_hash_validation(tmp_path: Path) -> None:
    """Test verifying an intact audit log."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")
    log.log("e2", "a2", "r2", "i2")

    valid, errors = log.verify()
    assert valid is True
    assert not errors


def test_audit_log_integrity_check_tamper_payload(tmp_path: Path) -> None:
    """Test that tampering with event payload is detected."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")

    # Tamper with the log file content (change actor)
    log_files = list(audit_dir.glob("*.jsonl"))
    content = log_files[0].read_text()
    tampered_content = content.replace('"a1"', '"tampered"')
    log_files[0].write_text(tampered_content)

    valid, errors = log.verify()
    assert valid is False
    assert any("HMAC mismatch" in err for err in errors)


def test_audit_log_integrity_check_broken_chain(tmp_path: Path) -> None:
    """Test that breaking the HMAC chain is detected."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("e1", "a1", "r1", "i1")
    log.log("e2", "a2", "r2", "i2")

    # Tamper with prev_hmac in second event record
    log_files = list(audit_dir.glob("*.jsonl"))
    lines = log_files[0].read_text().splitlines()
    data = json.loads(lines[1])
    data["prev_hmac"] = "0" * 64  # Incorrect prev_hmac
    lines[1] = json.dumps(data, sort_keys=True)
    log_files[0].write_text("\n".join(lines) + "\n")

    valid, errors = log.verify()
    assert valid is False
    assert any("prev_hmac mismatch" in err for err in errors)


def test_audit_log_query_filters(tmp_path: Path) -> None:
    """Test querying audit events with filters."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    log.log("type.A", "actor.1", "res", "id1")
    log.log("type.B", "actor.1", "res", "id2")
    log.log("type.A", "actor.2", "res", "id3")

    # Filter by type
    assert len(log.query(event_type="type.A")) == 2

    # Filter by actor
    assert len(log.query(actor="actor.1")) == 2

    # Filter by both
    results = log.query(event_type="type.A", actor="actor.1")
    assert len(results) == 1
    assert results[0].resource_id == "id1"
