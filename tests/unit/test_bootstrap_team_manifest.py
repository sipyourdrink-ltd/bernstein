"""Bootstrap lineage recording for team manifests (issue #2248, AC3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.seed import SeedConfig

from bernstein.core.orchestration.bootstrap import _record_team_manifest_lineage
from bernstein.core.security.audit import AuditLog
from bernstein.core.teams.audit import EVENT_RESOLVE
from bernstein.core.teams.lockfile import TEAMS_LOCK_FILENAME, read_state


@pytest.fixture(autouse=True)
def isolate_audit_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _seed(**kwargs: object) -> SeedConfig:
    return SeedConfig(goal="T", **kwargs)  # type: ignore[arg-type]


class TestRecordTeamManifestLineage:
    def test_records_event_and_lock_entry(self, tmp_path: Path) -> None:
        teams_dir = tmp_path / "templates" / "teams"
        teams_dir.mkdir(parents=True)
        (teams_dir / "crew.toml").write_text(
            'name = "crew"\nversion = "1.0.0"\n\n[[roles]]\nrole = "backend"\n',
            encoding="utf-8",
        )
        seed = _seed(team_manifest="crew", team_manifest_digest="ab" * 32)

        _record_team_manifest_lineage(seed, tmp_path)

        events = AuditLog(tmp_path / ".sdd" / "audit").query(event_type=EVENT_RESOLVE)
        assert len(events) == 1
        assert events[0].resource_id == "crew"
        assert events[0].details["digest"] == "ab" * 32
        assert read_state(tmp_path / TEAMS_LOCK_FILENAME).find("crew") is not None

    def test_noop_without_team_manifest(self, tmp_path: Path) -> None:
        _record_team_manifest_lineage(_seed(), tmp_path)
        assert not (tmp_path / ".sdd" / "audit").exists()
        assert not (tmp_path / TEAMS_LOCK_FILENAME).exists()

    def test_never_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import bernstein.core.orchestration.bootstrap as bootstrap_mod

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(
            "bernstein.core.teams.audit.record_run_team_manifest",
            _boom,
        )
        seed = _seed(team_manifest="crew", team_manifest_digest="ab" * 32)
        # Lineage anchoring is best-effort; a broken audit backend must not
        # abort the run.
        bootstrap_mod._record_team_manifest_lineage(seed, tmp_path)
