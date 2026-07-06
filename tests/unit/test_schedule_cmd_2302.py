"""CLI tests for ``schedule show --at`` and ``schedule verify`` (#2302).

``schedule show --at`` prints the graph hash a schedule would dispatch at
a given instant WITHOUT firing (AC2 observability, no side effects).
``schedule verify`` replays every recorded fire and confirms graph hash
equality (AC4). All state is isolated under ``tmp_path`` and the working
directory is set via ``monkeypatch.chdir`` so nothing leaks host cwd.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.schedule_cmd import schedule_group
from bernstein.core.orchestration.schedule_fire_record import record_fire
from bernstein.core.orchestration.schedule_projection import project
from bernstein.core.planning.schedule_store import ScheduleStore


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project cwd with an ``.sdd`` dir and a pinned audit key."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"deterministic-schedule-cli-key-32")
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    (tmp_path / ".sdd").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestShowAt:
    def test_prints_graph_hash_no_side_effects(self, project_root: Path) -> None:
        """AC2: schedule show --at prints the graph hash without side
        effects (no journal, no receipt, no last_fire mutation).
        """
        store = ScheduleStore(project_root / ".sdd")
        schedule = store.add(cron="0 9 * * *", goal="daily digest")

        runner = CliRunner()
        result = runner.invoke(schedule_group, ["show", schedule.id, "--at", "1700000000"])
        assert result.exit_code == 0, result.output

        expected = project(schedule.id, 1_700_000_000, None, goal="daily digest", recurrence="cron:0 9 * * *")
        assert result.output.strip() == expected.graph_hash

        # No fire was recorded, and last_fire_at stays 0.
        assert not (project_root / ".sdd" / "runs").exists()
        assert store.get(schedule.id).last_fire_at == 0.0

    def test_at_iso8601_accepted(self, project_root: Path) -> None:
        store = ScheduleStore(project_root / ".sdd")
        schedule = store.add(cron="0 9 * * *", goal="g")
        runner = CliRunner()
        epoch = runner.invoke(schedule_group, ["show", schedule.id, "--at", "1700000000"])
        iso = runner.invoke(schedule_group, ["show", schedule.id, "--at", "2023-11-14T22:13:20+00:00"])
        assert epoch.exit_code == 0
        assert iso.exit_code == 0
        assert epoch.output.strip() == iso.output.strip()

    def test_show_at_json(self, project_root: Path) -> None:
        store = ScheduleStore(project_root / ".sdd")
        schedule = store.add(cron="0 9 * * *", goal="g")
        runner = CliRunner()
        result = runner.invoke(schedule_group, ["show", schedule.id, "--at", "1700000000", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["graph_hash"]
        assert payload["fire_time"] == 1_700_000_000

    def test_two_operators_same_hash(self, project_root: Path) -> None:
        store = ScheduleStore(project_root / ".sdd")
        schedule = store.add(cron="0 9 * * *", goal="daily digest")
        runner = CliRunner()
        a = runner.invoke(schedule_group, ["show", schedule.id, "--at", "1700000000"])
        b = runner.invoke(schedule_group, ["show", schedule.id, "--at", "1700000000"])
        assert a.output.strip() == b.output.strip()


class TestVerify:
    def test_verify_matches_recorded_fire(self, project_root: Path) -> None:
        """AC4: schedule verify replays a past fire and confirms equality."""
        sdd = project_root / ".sdd"
        store = ScheduleStore(sdd)
        schedule = store.add(cron="0 9 * * *", goal="digest")
        record_fire(
            sdd_dir=sdd,
            schedule_id=schedule.id,
            fire_time=1_700_000_000,
            goal="digest",
            recurrence="cron:0 9 * * *",
        )
        runner = CliRunner()
        result = runner.invoke(schedule_group, ["verify"])
        assert result.exit_code == 0, result.output
        assert "ok" in result.output

    def test_verify_json_reports_ok(self, project_root: Path) -> None:
        sdd = project_root / ".sdd"
        store = ScheduleStore(sdd)
        schedule = store.add(cron="0 9 * * *", goal="digest")
        record_fire(sdd_dir=sdd, schedule_id=schedule.id, fire_time=1_700_000_000, goal="digest")
        runner = CliRunner()
        result = runner.invoke(schedule_group, ["verify", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert len(payload["fires"]) == 1

    def test_verify_detects_tamper(self, project_root: Path) -> None:
        sdd = project_root / ".sdd"
        store = ScheduleStore(sdd)
        schedule = store.add(cron="0 9 * * *", goal="digest")
        record_fire(sdd_dir=sdd, schedule_id=schedule.id, fire_time=1_700_000_000, goal="digest")

        # Tamper: rewrite the journal row's graph_hash to a wrong value.
        from bernstein.core.orchestration.schedule_fire_record import fire_run_id

        journal = sdd / "runs" / fire_run_id(schedule.id, 1_700_000_000) / "journal.jsonl"
        lines = journal.read_text().splitlines()
        rewritten: list[str] = []
        for line in lines:
            row = json.loads(line)
            if row.get("event") == "schedule.fire_projection":
                row["graph_hash"] = "0" * 64
            rewritten.append(json.dumps(row))
        journal.write_text("\n".join(rewritten) + "\n")

        runner = CliRunner()
        result = runner.invoke(schedule_group, ["verify"])
        assert result.exit_code == 1
        assert "MISMATCH" in result.output or "FAILED" in result.output

    def test_verify_no_fires(self, project_root: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(schedule_group, ["verify"])
        assert result.exit_code == 0
        assert "no schedule fires" in result.output
