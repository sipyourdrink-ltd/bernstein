"""``bernstein history --limit`` refuses a negative value and takes zero as "no records" (#7957).

``history_cmd`` called ``_history_rows(..., limit=max(1, limit))``, so ``--limit 0`` and
``--limit=-1`` both returned one archived task.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.maintenance_cmd import history_cmd

FILE = "note.txt"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    archive = tmp_path / ".sdd" / "archive"
    archive.mkdir(parents=True)
    records = [
        {
            "task_id": f"task-{n}",
            "title": f"edit note {n}",
            "role": "backend",
            "status": "done",
            "completed_at": 1_700_000_000.0 + n,
            "owned_files": [FILE],
        }
        for n in range(3)
    ]
    (archive / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return tmp_path


def _history(workdir: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(history_cmd, [FILE, "--workdir", str(workdir), *args])
    return result.exit_code, result.output


def _json_ids(out: str) -> list[str]:
    return [row["task_id"] for row in json.loads(out)["tasks"]]


@pytest.mark.parametrize("value", ["-1", "--limit=-5"])
def test_a_negative_limit_is_a_usage_error(workdir: Path, value: str) -> None:
    args = [value] if value.startswith("--") else ["--limit", value]
    code, out = _history(workdir, *args)
    assert code == 2
    assert "--limit" in out


def test_zero_returns_no_records_in_json(workdir: Path) -> None:
    code, out = _history(workdir, "--limit", "0", "--json")
    assert code == 0
    assert _json_ids(out) == []


def test_zero_returns_no_records_in_the_table(workdir: Path) -> None:
    code, out = _history(workdir, "--limit", "0")
    assert code == 0
    assert "No archived tasks" in out
    assert "task-" not in out


def test_a_positive_limit_returns_the_newest_matching_records(workdir: Path) -> None:
    code, out = _history(workdir, "--limit", "2", "--json")
    assert code == 0
    assert _json_ids(out) == ["task-2", "task-1"]
