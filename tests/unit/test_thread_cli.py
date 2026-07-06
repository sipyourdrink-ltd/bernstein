"""Unit tests for ``bernstein thread verify`` (issue #2297).

``thread verify --run <id>`` proves the streamed thread equals the run
journal: it recomputes the journal chain and the SSE projection and
reports a clean pass or the first divergent index (AC3). The helper is
exercised directly here; the click wiring is one indirection above.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.cli.commands.thread_cmd import thread_verify
from bernstein.core.replay.journal import EventJournal


def _run_journal(sdd_dir: Path, run_id: str = "run-cli", n: int = 3) -> EventJournal:
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    for i in range(n):
        journal.record("step", i=i)
    return journal


def test_thread_verify_clean_run_returns_0(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _run_journal(sdd, "run-cli", 3)

    rc = thread_verify(run_id="run-cli", sdd_dir=sdd, as_json=False)

    assert rc == 0


def test_thread_verify_missing_run_returns_2(tmp_path: Path) -> None:
    rc = thread_verify(run_id="nope", sdd_dir=tmp_path / ".sdd", as_json=False)
    assert rc == 2


def test_thread_verify_tampered_returns_1(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    journal = _run_journal(sdd, "run-cli", 3)
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"i": 1', '"i": 99')
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = thread_verify(run_id="run-cli", sdd_dir=sdd, as_json=False)

    assert rc == 1


def test_thread_verify_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sdd = tmp_path / ".sdd"
    _run_journal(sdd, "run-cli", 2)

    rc = thread_verify(run_id="run-cli", sdd_dir=sdd, as_json=True)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["run_id"] == "run-cli"
