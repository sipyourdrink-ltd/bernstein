"""Strict replay must refuse when the target run has no recording (#2790).

On the CLI-adapter path (``internal_llm_provider: none`` + a CLI agent) the
internal ``call_llm`` never runs, so no ``llm_calls.jsonl`` is written.
Activating replay against such a run must abort loudly before any agent is
spawned or any network call is made, instead of silently degrading to a live
run. ``open_replay_store`` is the activation-point guard that mirrors the
``ReplayMissError`` contract for the whole-run boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.orchestration.deterministic import (
    ReplayRecordingMissingError,
    open_replay_store,
)


def test_strict_replay_refuses_when_recording_absent(tmp_path: Path) -> None:
    """Strict replay against a run with no llm_calls.jsonl raises, naming the file."""
    run_dir = tmp_path / "runs" / "run-no-recording"
    run_dir.mkdir(parents=True)

    with pytest.raises(ReplayRecordingMissingError) as excinfo:
        open_replay_store(run_dir, strict=True)

    assert str(run_dir / "llm_calls.jsonl") in str(excinfo.value)


def test_non_strict_replay_allows_missing_recording(tmp_path: Path) -> None:
    """With live-miss allowed (non-strict), a missing recording does not abort."""
    run_dir = tmp_path / "runs" / "run-no-recording"
    run_dir.mkdir(parents=True)

    store = open_replay_store(run_dir, strict=False)
    assert store.cached_count == 0


def test_strict_replay_accepts_present_recording(tmp_path: Path) -> None:
    """A run with a non-empty recording opens normally under strict replay."""
    run_dir = tmp_path / "runs" / "run-with-recording"
    run_dir.mkdir(parents=True)
    (run_dir / "llm_calls.jsonl").write_text(
        json.dumps({"key": "k1", "response": "hello"}) + "\n",
        encoding="utf-8",
    )

    store = open_replay_store(run_dir, strict=True)
    assert store.cached_count == 1
