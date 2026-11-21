"""``bernstein recap --since`` counts tasks by ``completed_at``, the field the TaskStore writes (#7944).

The offline recap filtered ``.sdd/runtime/tasks.jsonl`` by a ``timestamp`` field. The TaskStore
records completion time as ``completed_at``, so a recent done task and a recent failed task counted
zero, while the same rows with ``timestamp`` counted one of each.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import recap

NOW = time.time()


def _workspace(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path


def _recap(workdir: Path) -> dict[str, object]:
    result = CliRunner().invoke(recap, ["--since", "1h", "--workdir", str(workdir), "--as-json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_recent_rows_with_completed_at_are_counted(tmp_path: Path) -> None:
    workdir = _workspace(
        tmp_path,
        [
            {"id": "done-1", "status": "done", "completed_at": NOW - 60},
            {"id": "fail-1", "status": "failed", "completed_at": NOW - 120},
            {"id": "old-1", "status": "done", "completed_at": NOW - 7200},
            {"id": "untimed", "status": "done"},
        ],
    )
    out = _recap(workdir)
    assert out["completed_tasks"] == 1
    assert out["failed_tasks"] == 1


def test_a_historical_timestamp_row_is_still_counted(tmp_path: Path) -> None:
    workdir = _workspace(
        tmp_path,
        [
            {"id": "legacy", "status": "done", "timestamp": NOW - 60},
            {"id": "legacy-old", "status": "failed", "timestamp": NOW - 7200},
        ],
    )
    out = _recap(workdir)
    assert out["completed_tasks"] == 1
    assert out["failed_tasks"] == 0
