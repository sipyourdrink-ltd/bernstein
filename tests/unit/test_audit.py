"""Tests for the HMAC-chained audit log — all file I/O via tmp_path."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bernstein.core.audit import _GENESIS_HMAC, AuditEvent, AuditLog

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "audit"
    d.mkdir()
    return d


@pytest.fixture()
def hmac_key() -> bytes:
    return b"test-hmac-key-for-unit-tests"


@pytest.fixture()
def audit_log(audit_dir: Path, hmac_key: bytes) -> AuditLog:
    return AuditLog(audit_dir, key=hmac_key)


# ---------------------------------------------------------------------------
# TestAuditLogWrite
# ---------------------------------------------------------------------------


class TestAuditLogWrite:
    def test_log_creates_daily_file(self, audit_log: AuditLog, audit_dir: Path) -> None:
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        audit_log.log("task.created", "spawner", "task", "abc123")

        log_file = audit_dir / f"{today}.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_log_returns_audit_event(self, audit_log: AuditLog) -> None:
        event = audit_log.log("task.created", "spawner", "task", "abc123", {"priority": 1})

        assert isinstance(event, AuditEvent)
        assert event.event_type == "task.created"
        assert event.actor == "spawner"
        assert event.resource_type == "task"
        assert event.resource_id == "abc123"
        assert event.details == {"priority": 1}
        assert event.hmac != ""
        assert event.prev_hmac == _GENESIS_HMAC

    def test_log_chains_hmacs(self, audit_log: AuditLog) -> None:
        e1 = audit_log.log("task.created", "spawner", "task", "t1")
        e2 = audit_log.log("task.claimed", "worker", "task", "t1")

        assert e1.hmac != e2.hmac
        assert e2.prev_hmac == e1.hmac

    def test_log_appends_to_same_day_file(self, audit_log: AuditLog, audit_dir: Path) -> None:
        audit_log.log("ev1", "a", "r", "1")
        audit_log.log("ev2", "b", "r", "2")
        audit_log.log("ev3", "c", "r", "3")

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        log_file = audit_dir / f"{today}.jsonl"
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# TestAuditLogVerify
# ---------------------------------------------------------------------------


class TestAuditLogVerify:
    def test_verify_empty_log(self, audit_log: AuditLog) -> None:
        valid, errors = audit_log.verify()
        assert valid is True
        assert errors == []

    def test_verify_intact_chain(self, audit_log: AuditLog) -> None:
        audit_log.log("ev1", "actor1", "task", "t1")
        audit_log.log("ev2", "actor2", "task", "t2")
        audit_log.log("ev3", "actor3", "agent", "a1")

        valid, errors = audit_log.verify()
        assert valid is True
        assert errors == []

    def test_verify_detects_tampered_entry(self, audit_log: AuditLog, audit_dir: Path) -> None:
        audit_log.log("ev1", "actor1", "task", "t1")
        audit_log.log("ev2", "actor2", "task", "t2")

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        log_file = audit_dir / f"{today}.jsonl"
        lines = log_file.read_text().strip().splitlines()

        entry = json.loads(lines[0])
        entry["actor"] = "EVIL_ACTOR"
        lines[0] = json.dumps(entry, sort_keys=True)
        log_file.write_text("\n".join(lines) + "\n")

        valid, errors = audit_log.verify()
        assert valid is False
        assert len(errors) >= 1
        assert any("HMAC mismatch" in e for e in errors)

    def test_verify_detects_deleted_entry(self, audit_log: AuditLog, audit_dir: Path) -> None:
        audit_log.log("ev1", "actor1", "task", "t1")
        audit_log.log("ev2", "actor2", "task", "t2")
        audit_log.log("ev3", "actor3", "task", "t3")

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        log_file = audit_dir / f"{today}.jsonl"
        lines = log_file.read_text().strip().splitlines()
        log_file.write_text(lines[0] + "\n" + lines[2] + "\n")

        valid, errors = audit_log.verify()
        assert valid is False
        assert len(errors) >= 1

    def test_verify_detects_inserted_entry(self, audit_log: AuditLog, audit_dir: Path) -> None:
        audit_log.log("ev1", "actor1", "task", "t1")

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        log_file = audit_dir / f"{today}.jsonl"
        content = log_file.read_text()
        fake_entry = json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00.000000Z",
                "event_type": "fake",
                "actor": "intruder",
                "resource_type": "task",
                "resource_id": "x",
                "details": {},
                "prev_hmac": _GENESIS_HMAC,
                "hmac": "deadbeef" * 8,
            },
            sort_keys=True,
        )
        log_file.write_text(content + fake_entry + "\n")

        valid, _errors = audit_log.verify()
        assert valid is False


# ---------------------------------------------------------------------------
# TestDailyRotation
# ---------------------------------------------------------------------------


class TestDailyRotation:
    def test_chain_carries_across_files(self, audit_dir: Path, hmac_key: bytes) -> None:
        """prev_hmac of the first entry in day-2 must equal the last HMAC of day-1."""
        log1 = AuditLog(audit_dir, key=hmac_key)

        with patch("bernstein.core.audit.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            e1 = log1.log("ev1", "a", "task", "t1")

        day1_file = audit_dir / "2025-06-01.jsonl"
        assert day1_file.exists()

        with patch("bernstein.core.audit.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 6, 2, 8, 0, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            e2 = log1.log("ev2", "b", "task", "t2")

        day2_file = audit_dir / "2025-06-02.jsonl"
        assert day2_file.exists()

        assert e2.prev_hmac == e1.hmac

        valid, errors = log1.verify()
        assert valid is True, errors

    def test_new_instance_recovers_chain(self, audit_dir: Path, hmac_key: bytes) -> None:
        """A fresh AuditLog instance picks up the chain tail from existing files."""
        log1 = AuditLog(audit_dir, key=hmac_key)
        e1 = log1.log("first", "actor", "task", "t1")

        log2 = AuditLog(audit_dir, key=hmac_key)
        e2 = log2.log("second", "actor", "task", "t2")

        assert e2.prev_hmac == e1.hmac

        valid, errors = log2.verify()
        assert valid is True, errors


# ---------------------------------------------------------------------------
# TestQuery
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_all(self, audit_log: AuditLog) -> None:
        audit_log.log("ev1", "a", "task", "t1")
        audit_log.log("ev2", "b", "agent", "a1")

        results = audit_log.query()
        assert len(results) == 2

    def test_query_by_event_type(self, audit_log: AuditLog) -> None:
        audit_log.log("task.created", "spawner", "task", "t1")
        audit_log.log("agent.started", "orchestrator", "agent", "a1")
        audit_log.log("task.completed", "worker", "task", "t2")

        results = audit_log.query(event_type="task.created")
        assert len(results) == 1
        assert results[0].event_type == "task.created"

    def test_query_by_actor(self, audit_log: AuditLog) -> None:
        audit_log.log("ev1", "spawner", "task", "t1")
        audit_log.log("ev2", "worker", "task", "t2")
        audit_log.log("ev3", "spawner", "task", "t3")

        results = audit_log.query(actor="spawner")
        assert len(results) == 2

    def test_query_by_since(self, audit_log: AuditLog) -> None:
        audit_log.log("ev1", "a", "task", "t1")
        audit_log.log("ev2", "a", "task", "t2")

        results = audit_log.query(since="2020-01-01T00:00:00Z")
        assert len(results) == 2

        results = audit_log.query(since="2099-01-01T00:00:00Z")
        assert len(results) == 0

    def test_query_combined_filters(self, audit_log: AuditLog) -> None:
        audit_log.log("task.created", "spawner", "task", "t1")
        audit_log.log("task.created", "worker", "task", "t2")
        audit_log.log("agent.started", "spawner", "agent", "a1")

        results = audit_log.query(event_type="task.created", actor="spawner")
        assert len(results) == 1
        assert results[0].resource_id == "t1"


# ---------------------------------------------------------------------------
# TestKeyManagement
# ---------------------------------------------------------------------------


class TestKeyManagement:
    def test_auto_generates_key(self, audit_dir: Path) -> None:
        AuditLog(audit_dir)
        key_path = audit_dir.parent / "config" / "audit-key"
        assert key_path.exists()
        assert len(key_path.read_bytes().strip()) > 0

    def test_reuses_existing_key(self, audit_dir: Path) -> None:
        log1 = AuditLog(audit_dir)
        log1.log("ev1", "a", "t", "1")

        log2 = AuditLog(audit_dir)
        valid, errors = log2.verify()
        assert valid is True, errors
