"""Guard: only one canonical run recorder remains (issue #2293, AC3).

The orchestrator ``RunRecorder`` and the second ``replay.jsonl`` recorder
path were consolidated into the single Merkle-chained ``EventJournal``.
These tests fail if either resurfaces.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"


def _grep(pattern: str) -> list[str]:
    result = subprocess.run(
        ["grep", "-rn", "--include=*.py", pattern, str(_SRC)],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_run_recorder_class_is_removed() -> None:
    """No ``RunRecorder`` class definition survives in the package."""
    hits = _grep("class RunRecorder")
    assert hits == [], f"RunRecorder class should be gone; found: {hits}"


def test_orchestrator_does_not_construct_run_recorder() -> None:
    """The orchestrator constructs the journal, never RunRecorder."""
    hits = _grep("RunRecorder(")
    assert hits == [], f"RunRecorder construction should be gone; found: {hits}"


def test_no_second_replay_jsonl_recorder_path() -> None:
    """No code writes to a ``replay.jsonl`` per-run recorder path.

    The canonical journal writes ``journal.jsonl``. Docstrings that merely
    describe the historical file are allowed; an actual ``"replay.jsonl"``
    path literal in code is not.
    """
    hits = [line for line in _grep('"replay.jsonl"') if "test" not in line]
    assert hits == [], f"replay.jsonl recorder path should be gone; found: {hits}"


def test_orchestrator_uses_event_journal() -> None:
    """The orchestrator wires the canonical EventJournal as its recorder."""
    orchestrator = (_SRC / "core" / "orchestration" / "orchestrator.py").read_text()
    assert "EventJournal(run_id=run_id" in orchestrator
