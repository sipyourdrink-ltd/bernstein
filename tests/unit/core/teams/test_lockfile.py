"""Tests for bernstein.core.teams.lockfile (issue #2248)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.skills.catalog.lockfile import (
    RECEIPT_ADOPT,
    RECEIPT_INSTALL,
    RECEIPT_PIN,
)
from bernstein.core.teams.lockfile import (
    TEAMS_LOCK_FILENAME,
    TeamLockEntry,
    read_state,
    upsert_team_pin,
    write_state,
)


def _entry(name: str = "python", chain_head: str = "cc" * 32, digest: str = "ab" * 32) -> TeamLockEntry:
    return TeamLockEntry(
        name=name,
        version="1.0.0",
        manifest_digest=digest,
        source="templates/teams/python.toml",
        install_id="deadbeef",
        chain_head=chain_head,
        installed_at="2026-07-05T00:00:00+00:00",
    )


class TestTeamLockRoundTrip:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        lock_path = tmp_path / TEAMS_LOCK_FILENAME
        state = upsert_team_pin(lock_path, _entry(), workdir=tmp_path)
        reread = read_state(lock_path)
        assert reread.teams == state.teams
        assert reread.find("python") is not None
        assert reread.find("python").manifest_digest == "ab" * 32

    def test_missing_lockfile_reads_empty(self, tmp_path: Path) -> None:
        state = read_state(tmp_path / TEAMS_LOCK_FILENAME)
        assert state.teams == []
        assert state.receipts == []

    def test_write_is_deterministic(self, tmp_path: Path) -> None:
        lock_path_a = tmp_path / "a" / TEAMS_LOCK_FILENAME
        lock_path_b = tmp_path / "b" / TEAMS_LOCK_FILENAME
        state = upsert_team_pin(lock_path_a, _entry(), workdir=tmp_path)
        write_state(lock_path_b, state)
        assert lock_path_b.read_text(encoding="utf-8") == lock_path_a.read_text(encoding="utf-8")


class TestReceipts:
    def test_first_upsert_emits_install_receipt(self, tmp_path: Path) -> None:
        state = upsert_team_pin(tmp_path / TEAMS_LOCK_FILENAME, _entry(), workdir=tmp_path)
        assert [r.action for r in state.receipts] == [RECEIPT_INSTALL]
        assert state.receipts[0].entry_id == "python"
        assert state.receipts[0].to_chain_head == "cc" * 32

    def test_same_chain_head_emits_pin_receipt(self, tmp_path: Path) -> None:
        lock_path = tmp_path / TEAMS_LOCK_FILENAME
        upsert_team_pin(lock_path, _entry(), workdir=tmp_path)
        state = upsert_team_pin(lock_path, _entry(), workdir=tmp_path)
        assert [r.action for r in state.receipts] == [RECEIPT_INSTALL, RECEIPT_PIN]

    def test_new_chain_head_emits_adopt_receipt(self, tmp_path: Path) -> None:
        lock_path = tmp_path / TEAMS_LOCK_FILENAME
        upsert_team_pin(lock_path, _entry(), workdir=tmp_path)
        state = upsert_team_pin(lock_path, _entry(chain_head="dd" * 32), workdir=tmp_path)
        assert [r.action for r in state.receipts] == [RECEIPT_INSTALL, RECEIPT_ADOPT]
        adopt = state.receipts[-1]
        assert adopt.from_chain_head == "cc" * 32
        assert adopt.to_chain_head == "dd" * 32


class TestStateDigest:
    def test_same_entries_yield_same_digest(self, tmp_path: Path) -> None:
        state_a = upsert_team_pin(tmp_path / "a.lock", _entry(), workdir=tmp_path)
        state_b = upsert_team_pin(tmp_path / "b.lock", _entry(), workdir=tmp_path / "elsewhere")
        assert state_a.digest() == state_b.digest()

    def test_different_manifest_digest_changes_state_digest(self, tmp_path: Path) -> None:
        state_a = upsert_team_pin(tmp_path / "a.lock", _entry(), workdir=tmp_path)
        state_b = upsert_team_pin(tmp_path / "b.lock", _entry(digest="ee" * 32), workdir=tmp_path)
        assert state_a.digest() != state_b.digest()
