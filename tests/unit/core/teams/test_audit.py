"""Tests for bernstein.core.teams.audit (issue #2248, acceptance criterion 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security.audit import AuditLog
from bernstein.core.teams.audit import (
    EVENT_RESOLVE,
    TeamManifestAuditor,
    record_run_team_manifest,
)
from bernstein.core.teams.lockfile import TEAMS_LOCK_FILENAME, read_state


@pytest.fixture(autouse=True)
def isolate_audit_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _write_local_manifest(tmp_path: Path) -> None:
    teams_dir = tmp_path / "templates" / "teams"
    teams_dir.mkdir(parents=True)
    (teams_dir / "crew.toml").write_text(
        'name = "crew"\nversion = "2.0.0"\n\n[[roles]]\nrole = "backend"\n',
        encoding="utf-8",
    )


class TestRecordRunTeamManifest:
    def test_records_resolve_event_in_audit_chain(self, tmp_path: Path) -> None:
        _write_local_manifest(tmp_path)
        record_run_team_manifest(tmp_path, name="crew", digest="ab" * 32)

        log = AuditLog(tmp_path / ".sdd" / "audit")
        events = log.query(event_type=EVENT_RESOLVE)
        assert len(events) == 1
        event = events[0]
        assert event.resource_id == "crew"
        assert event.details["digest"] == "ab" * 32
        assert event.details["name"] == "crew"

    def test_records_teams_lock_entry_with_chain_head(self, tmp_path: Path) -> None:
        _write_local_manifest(tmp_path)
        record_run_team_manifest(tmp_path, name="crew", digest="ab" * 32)

        state = read_state(tmp_path / TEAMS_LOCK_FILENAME)
        entry = state.find("crew")
        assert entry is not None
        assert entry.manifest_digest == "ab" * 32
        assert entry.version == "2.0.0"
        assert entry.chain_head != ""
        assert len(state.receipts) == 1

    def test_unresolvable_manifest_still_records_event(self, tmp_path: Path) -> None:
        record_run_team_manifest(tmp_path, name="ghost", digest="ab" * 32)
        log = AuditLog(tmp_path / ".sdd" / "audit")
        events = log.query(event_type=EVENT_RESOLVE)
        assert len(events) == 1
        assert events[0].resource_id == "ghost"

    def test_never_raises_on_unwritable_lockfile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_local_manifest(tmp_path)

        import bernstein.core.teams.audit as teams_audit

        def _boom(*args: object, **kwargs: object) -> object:
            raise OSError("disk full")

        monkeypatch.setattr(teams_audit, "upsert_team_pin", _boom)
        # Must not propagate: lineage recording is best-effort at run start.
        record_run_team_manifest(tmp_path, name="crew", digest="ab" * 32)


class TestTeamManifestAuditor:
    def test_disabled_auditor_is_noop(self) -> None:
        auditor = TeamManifestAuditor(audit_dir=None)
        assert not auditor.enabled
        assert auditor.resolve(name="crew", digest="ab" * 32, version="1", source="x") is None

    def test_resolve_event_shape(self, tmp_path: Path) -> None:
        auditor = TeamManifestAuditor(audit_dir=tmp_path / "audit")
        event = auditor.resolve(name="crew", digest="ab" * 32, version="1.0.0", source="local")
        assert event is not None
        assert event.event_type == EVENT_RESOLVE
        assert event.details == {
            "name": "crew",
            "digest": "ab" * 32,
            "version": "1.0.0",
            "source": "local",
        }
