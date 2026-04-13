"""Tests for ENT-003: Audit log integrity verification on startup."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from bernstein.core.audit_integrity import (
    verify_audit_integrity,
    verify_on_startup,
)

_GENESIS_HMAC = "0" * 64


def _compute_test_hmac(key: bytes, prev_hmac: str, entry: dict[str, object]) -> str:
    """Match audit.py's _compute_hmac."""
    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _write_audit_chain(
    audit_dir: Path,
    key: bytes,
    count: int,
    filename: str = "2026-04-05.jsonl",
) -> list[dict[str, object]]:
    """Write a valid HMAC-chained audit log with *count* entries."""
    entries: list[dict[str, object]] = []
    prev = _GENESIS_HMAC
    lines: list[str] = []
    for i in range(count):
        entry: dict[str, object] = {
            "timestamp": f"2026-04-05T00:00:{i:02d}.000000Z",
            "event_type": "test.event",
            "actor": "test-actor",
            "resource_type": "task",
            "resource_id": f"task-{i}",
            "details": {},
            "prev_hmac": prev,
        }
        computed = _compute_test_hmac(key, prev, entry)
        entry["hmac"] = computed
        entries.append(entry)
        lines.append(json.dumps(entry, sort_keys=True))
        prev = computed

    (audit_dir / filename).write_text("\n".join(lines) + "\n")
    return entries


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


@pytest.fixture()
def audit_dir(sdd_dir: Path) -> Path:
    d = sdd_dir / "audit"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def hmac_key(sdd_dir: Path) -> bytes:
    key = b"test-hmac-key-for-audit"
    key_dir = sdd_dir / "config"
    key_dir.mkdir(parents=True)
    (key_dir / "audit-key").write_bytes(key)
    return key


class TestVerifyAuditIntegrity:
    """Test audit log integrity verification."""

    def test_no_audit_dir(self, sdd_dir: Path) -> None:
        result = verify_audit_integrity(sdd_dir / "nonexistent")
        assert result.valid is True
        assert result.entries_checked == 0
        assert len(result.warnings) > 0

    def test_no_key_file(self, audit_dir: Path) -> None:
        (audit_dir / "2026-04-05.jsonl").write_text('{"event": "test"}\n')
        result = verify_audit_integrity(audit_dir)
        assert result.valid is True
        assert result.entries_checked == 0
        assert any("key not found" in w for w in result.warnings)

    def test_empty_audit_dir(self, audit_dir: Path, hmac_key: bytes) -> None:
        result = verify_audit_integrity(audit_dir, key=hmac_key)
        assert result.valid is True
        assert result.entries_checked == 0
        assert any("No audit entries" in w for w in result.warnings)

    def test_valid_chain(self, audit_dir: Path, hmac_key: bytes) -> None:
        _write_audit_chain(audit_dir, hmac_key, 10)
        result = verify_audit_integrity(audit_dir, count=10, key=hmac_key)
        assert result.valid is True
        assert result.entries_checked == 10
        assert result.entries_total == 10
        assert result.errors == []

    def test_valid_chain_partial_check(self, audit_dir: Path, hmac_key: bytes) -> None:
        _write_audit_chain(audit_dir, hmac_key, 20)
        result = verify_audit_integrity(audit_dir, count=5, key=hmac_key)
        assert result.valid is True
        assert result.entries_checked == 5
        assert result.entries_total == 20

    def test_tampered_hmac(self, audit_dir: Path, hmac_key: bytes) -> None:
        entries = _write_audit_chain(audit_dir, hmac_key, 5)
        # Tamper with the 3rd entry's HMAC
        entries[2]["hmac"] = "deadbeef" * 8
        lines = [json.dumps(e, sort_keys=True) for e in entries]
        (audit_dir / "2026-04-05.jsonl").write_text("\n".join(lines) + "\n")

        result = verify_audit_integrity(audit_dir, count=5, key=hmac_key)
        assert result.valid is False
        assert len(result.errors) >= 1
        assert any("HMAC mismatch" in e for e in result.errors)

    def test_broken_chain(self, audit_dir: Path, hmac_key: bytes) -> None:
        entries = _write_audit_chain(audit_dir, hmac_key, 5)
        # Break the chain by modifying prev_hmac of entry 3
        entries[2]["prev_hmac"] = "0" * 64
        # Recompute that entry's HMAC with the wrong prev
        recomputed = _compute_test_hmac(
            hmac_key,
            entries[2]["prev_hmac"],
            {k: v for k, v in entries[2].items() if k != "hmac"},  # type: ignore[arg-type]
        )
        entries[2]["hmac"] = recomputed
        lines = [json.dumps(e, sort_keys=True) for e in entries]
        (audit_dir / "2026-04-05.jsonl").write_text("\n".join(lines) + "\n")

        result = verify_audit_integrity(audit_dir, count=5, key=hmac_key)
        assert result.valid is False
        assert any("chain broken" in e for e in result.errors)

    def test_multiple_files(self, audit_dir: Path, hmac_key: bytes) -> None:
        # Write entries across two files
        _write_audit_chain(audit_dir, hmac_key, 5, filename="2026-04-04.jsonl")
        # Note: second file's chain starts fresh (we're only checking tail)
        _write_audit_chain(audit_dir, hmac_key, 5, filename="2026-04-05.jsonl")
        result = verify_audit_integrity(audit_dir, count=5, key=hmac_key)
        assert result.valid is True
        assert result.entries_checked == 5

    def test_duration_tracked(self, audit_dir: Path, hmac_key: bytes) -> None:
        _write_audit_chain(audit_dir, hmac_key, 3)
        result = verify_audit_integrity(audit_dir, count=3, key=hmac_key)
        assert result.duration_ms >= 0.0

    def test_checked_at_populated(self, audit_dir: Path, hmac_key: bytes) -> None:
        _write_audit_chain(audit_dir, hmac_key, 1)
        result = verify_audit_integrity(audit_dir, count=1, key=hmac_key)
        assert result.checked_at != ""


class TestVerifyOnStartup:
    """Test the startup convenience wrapper."""

    def test_no_sdd_dir(self, tmp_path: Path) -> None:
        result = verify_on_startup(tmp_path / "nonexistent")
        assert result.valid is True

    def test_valid_audit(self, sdd_dir: Path, audit_dir: Path, hmac_key: bytes) -> None:
        _write_audit_chain(audit_dir, hmac_key, 10)
        result = verify_on_startup(sdd_dir, count=10)
        assert result.valid is True
        assert result.entries_checked == 10

    def test_tampered_audit_warns(self, sdd_dir: Path, audit_dir: Path, hmac_key: bytes) -> None:
        entries = _write_audit_chain(audit_dir, hmac_key, 5)
        entries[1]["hmac"] = "bad" * 21 + "b"
        lines = [json.dumps(e, sort_keys=True) for e in entries]
        (audit_dir / "2026-04-05.jsonl").write_text("\n".join(lines) + "\n")
        result = verify_on_startup(sdd_dir, count=5)
        assert result.valid is False
