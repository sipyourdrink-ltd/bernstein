"""An abandonment row whose text holds U+2028 is read as one row (#7960).

The ledger read its file with ``str.splitlines()``, which also splits on U+2028 and U+2029. A row
written with ``ensure_ascii=False`` and a line separator in ``detail`` became two halves of invalid
JSON, so ``abandonments list`` omitted it and ``stats`` counted one row too few.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.abandonments_cmd import abandonments_group
from bernstein.core.tasks.abandon import AbandonmentLedger

DETAIL = "scope grew\u2028after review"
ROWS = [
    {"id": "a1", "task_id": "t1", "reason": "out_of_scope", "detail": "first", "timestamp": 100.0},
    {"id": "a2", "task_id": "t2", "reason": "out_of_scope", "detail": DETAIL, "timestamp": 200.0},
    {"id": "a3", "task_id": "t3", "reason": "insufficient_context", "detail": "third", "timestamp": 300.0},
]


def _write(workdir: Path, *, ensure_ascii: bool, extra: str = "") -> Path:
    runtime = workdir / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / "abandonments.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=ensure_ascii) + "\n" for r in ROWS) + extra, encoding="utf-8")
    return path


def _cli(workdir: Path, *args: str) -> object:
    result = CliRunner().invoke(abandonments_group, [*args, "--workdir", str(workdir), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_both_encodings_list_the_same_three_rows(tmp_path: Path, ensure_ascii: bool) -> None:
    _write(tmp_path, ensure_ascii=ensure_ascii)
    rows = _cli(tmp_path, "list")
    assert isinstance(rows, list)
    assert [r["id"] for r in rows] == ["a3", "a2", "a1"]
    assert rows[1]["detail"] == DETAIL


@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_both_encodings_give_the_same_stats(tmp_path: Path, ensure_ascii: bool) -> None:
    _write(tmp_path, ensure_ascii=ensure_ascii)
    stats = _cli(tmp_path, "stats")
    assert isinstance(stats, dict)
    assert stats["total"] == 3
    assert stats["by_reason"] == {"out_of_scope": 2, "insufficient_context": 1}


def test_a_malformed_row_is_still_skipped(tmp_path: Path) -> None:
    _write(tmp_path, ensure_ascii=False, extra="{not json\n")
    assert [r.id for r in AbandonmentLedger(tmp_path / ".sdd").read_all()] == ["a1", "a2", "a3"]
