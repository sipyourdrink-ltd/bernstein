"""Tests for the fleet audit panel and tail-break detection."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.fleet.audit import (
    check_audit_tail,
    filter_audit_entries,
    load_recent_entries,
)


def _write_audit(
    sdd: Path,
    entries: list[dict[str, object]],
    *,
    filename: str = "2026-04-25.jsonl",
) -> None:
    audit_dir = sdd / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / filename).open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_check_audit_tail_no_dir(tmp_path: Path) -> None:
    """A project with no audit directory is treated as fresh, not broken."""
    status = check_audit_tail("alpha", tmp_path / ".sdd")
    assert status.ok is True
    assert "no audit log" in status.message


def test_check_audit_tail_intact_chain(tmp_path: Path) -> None:
    """A correctly linked chain reports ok."""
    sdd = tmp_path / ".sdd"
    _write_audit(
        sdd,
        [
            {"role": "manager", "ts": 1.0, "hmac": "h1", "prev_hmac": "0" * 64},
            {"role": "backend", "ts": 2.0, "hmac": "h2", "prev_hmac": "h1"},
            {"role": "qa", "ts": 3.0, "hmac": "h3", "prev_hmac": "h2"},
        ],
    )
    status = check_audit_tail("alpha", sdd)
    assert status.ok is True
    assert status.entries_checked == 3


def test_check_audit_tail_break(tmp_path: Path) -> None:
    """A mismatched ``prev_hmac`` is flagged with a precise location."""
    sdd = tmp_path / ".sdd"
    _write_audit(
        sdd,
        [
            {"role": "manager", "ts": 1.0, "hmac": "h1", "prev_hmac": "0" * 64},
            {"role": "backend", "ts": 2.0, "hmac": "h2", "prev_hmac": "h1"},
            # break: prev_hmac should be h2, not garbage.
            {"role": "qa", "ts": 3.0, "hmac": "h3", "prev_hmac": "garbage"},
        ],
    )
    status = check_audit_tail("alpha", sdd)
    assert status.ok is False
    assert status.broken_at is not None
    assert "chain break" in status.message


def test_load_and_filter_entries(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _write_audit(
        sdd,
        [
            {"role": "manager", "ts": 1.0, "adapter": "claude", "outcome": "ok", "hmac": "h1", "prev_hmac": "0" * 64},
            {"role": "backend", "ts": 2.0, "adapter": "codex", "outcome": "denied", "hmac": "h2", "prev_hmac": "h1"},
            {"role": "qa", "ts": 3.0, "adapter": "claude", "outcome": "ok", "hmac": "h3", "prev_hmac": "h2"},
        ],
    )
    entries = load_recent_entries("alpha", sdd, max_entries=10)
    assert len(entries) == 3
    by_role = filter_audit_entries(entries, role="backend")
    assert len(by_role) == 1
    assert by_role[0].outcome == "denied"

    by_adapter = filter_audit_entries(entries, adapter="claude")
    assert {e.role for e in by_adapter} == {"manager", "qa"}

    by_time = filter_audit_entries(entries, since=2.5)
    assert len(by_time) == 1
    assert by_time[0].role == "qa"
