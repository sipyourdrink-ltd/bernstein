"""Unit tests for bernstein retro CLI command."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import _build_collector_from_archive, _load_archive_tasks, cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_archive(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _make_record(
    task_id: str,
    title: str,
    role: str,
    status: str,
    created_at: float,
    completed_at: float,
    cost_usd: float | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "title": title,
        "role": role,
        "status": status,
        "created_at": created_at,
        "completed_at": completed_at,
        "duration_seconds": completed_at - created_at,
        "result_summary": "ok",
        "cost_usd": cost_usd,
    }


NOW = time.time()

SAMPLE_RECORDS = [
    _make_record("aaa111", "Fix login bug", "backend", "done", NOW - 3600, NOW - 3500, 0.01),
    _make_record("bbb222", "Add health check", "backend", "done", NOW - 2000, NOW - 1900, 0.02),
    _make_record("ccc333", "Write tests", "qa", "failed", NOW - 1800, NOW - 1700, 0.005),
    _make_record("ddd444", "Old task", "backend", "done", NOW - 100000, NOW - 99900, 0.03),
]


# ---------------------------------------------------------------------------
# _load_archive_tasks
# ---------------------------------------------------------------------------


def test_load_archive_tasks_all(tmp_path: Path) -> None:
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    done, failed = _load_archive_tasks(archive, since_ts=None)

    assert len(done) == 3
    assert len(failed) == 1
    assert all(t.status.value == "done" for t in done)
    assert all(t.status.value == "failed" for t in failed)


def test_load_archive_tasks_since_filter(tmp_path: Path) -> None:
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    # Only include tasks completed in the last 24h (excludes ddd444 which is ~28h old)
    since_ts = NOW - 24 * 3600
    done, failed = _load_archive_tasks(archive, since_ts=since_ts)

    task_ids = {t.id for t in done + failed}
    assert "ddd444" not in task_ids
    assert "aaa111" in task_ids
    assert "bbb222" in task_ids
    assert "ccc333" in task_ids


def test_load_archive_tasks_missing_file(tmp_path: Path) -> None:
    archive = tmp_path / "nonexistent.jsonl"
    done, failed = _load_archive_tasks(archive, since_ts=None)
    assert done == []
    assert failed == []


def test_load_archive_tasks_skips_invalid_json(tmp_path: Path) -> None:
    archive = tmp_path / "tasks.jsonl"
    archive.write_text(
        '{"task_id":"x","status":"done","role":"r","title":"t","created_at":1.0,"completed_at":2.0}\nnot-json\n'
    )
    done, failed = _load_archive_tasks(archive, since_ts=None)
    assert len(done) == 1


def test_load_archive_tasks_skips_non_terminal_status(tmp_path: Path) -> None:
    archive = tmp_path / "tasks.jsonl"
    records = [
        _make_record("x1", "T1", "backend", "open", NOW - 100, NOW - 50),
        _make_record("x2", "T2", "backend", "in_progress", NOW - 100, NOW - 50),
        _make_record("x3", "T3", "backend", "done", NOW - 100, NOW - 50),
    ]
    _write_archive(archive, records)
    done, failed = _load_archive_tasks(archive, since_ts=None)
    assert len(done) == 1
    assert done[0].id == "x3"


# ---------------------------------------------------------------------------
# _build_collector_from_archive
# ---------------------------------------------------------------------------


def test_build_collector_from_archive(tmp_path: Path) -> None:
    archive = tmp_path / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    collector = _build_collector_from_archive(archive, since_ts=None)

    # All 4 records should be in task_metrics
    assert len(collector._task_metrics) == 4


def test_build_collector_since_filter(tmp_path: Path) -> None:
    archive = tmp_path / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    since_ts = NOW - 24 * 3600
    collector = _build_collector_from_archive(archive, since_ts=since_ts)

    assert len(collector._task_metrics) == 3
    assert "ddd444" not in collector._task_metrics


def test_build_collector_cost_populated(tmp_path: Path) -> None:
    archive = tmp_path / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS[:2])  # cost 0.01 + 0.02 = 0.03

    collector = _build_collector_from_archive(archive, since_ts=None)
    # get_total_cost sums agent_metrics; verify task_metrics has cost set
    total = sum(tm.cost_usd for tm in collector._task_metrics.values())
    assert total == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# retro CLI command
# ---------------------------------------------------------------------------


def test_retro_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    runner = CliRunner()
    result = runner.invoke(cli, ["retro"])

    assert result.exit_code == 0
    retro_path = tmp_path / ".sdd" / "runtime" / "retrospective.md"
    assert retro_path.exists()
    content = retro_path.read_text()
    assert "# Run Retrospective" in content
    assert "Completion rate" in content


def test_retro_custom_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)
    out = tmp_path / "my_report.md"

    runner = CliRunner()
    result = runner.invoke(cli, ["retro", "--output", str(out)])

    assert result.exit_code == 0
    assert out.exists()
    assert "# Run Retrospective" in out.read_text()


def test_retro_print_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    runner = CliRunner()
    result = runner.invoke(cli, ["retro", "--print"])

    assert result.exit_code == 0
    assert "# Run Retrospective" in result.output


def test_retro_no_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    # No archive file

    runner = CliRunner()
    result = runner.invoke(cli, ["retro"])

    assert result.exit_code == 0
    assert "No completed or failed tasks" in result.output


def test_retro_since_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    _write_archive(archive, SAMPLE_RECORDS)

    runner = CliRunner()
    # --since 1 should only include tasks from the last 1 hour
    result = runner.invoke(cli, ["retro", "--since", "1", "--print"])

    assert result.exit_code == 0
    # ddd444 is ~28h old, should not appear; bbb222/ccc333 should appear
    content = result.output
    assert "# Run Retrospective" in content
