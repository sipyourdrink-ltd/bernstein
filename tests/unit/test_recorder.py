"""Focused tests for replay recording helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.recorder import (
    RunRecorder,
    compute_replay_fingerprint,
    load_replay_events,
)


def test_record_writes_event_with_elapsed_time(tmp_path: Path) -> None:
    """RunRecorder.record appends a JSONL event with timestamped metadata."""
    with patch("bernstein.core.persistence.recorder.time.time", side_effect=[100.0, 100.25, 100.5]):
        recorder = RunRecorder(run_id="run-1", sdd_dir=tmp_path)
        recorder.record("task_claimed", task_id="T-1", agent_id="A-1")

    lines = recorder.path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    assert payload["event"] == "task_claimed"
    assert payload["task_id"] == "T-1"
    assert payload["agent_id"] == "A-1"
    assert payload["elapsed_s"] == pytest.approx(0.5)


def test_fingerprint_changes_when_new_events_are_added(tmp_path: Path) -> None:
    """RunRecorder.fingerprint reflects the full replay stream contents."""
    recorder = RunRecorder(run_id="run-2", sdd_dir=tmp_path)
    recorder.record("task_claimed", task_id="T-1")
    first = recorder.fingerprint()

    recorder.record("task_completed", task_id="T-1")
    second = recorder.fingerprint()

    assert first
    assert second
    assert first != second


def test_event_count_ignores_blank_lines(tmp_path: Path) -> None:
    """RunRecorder.event_count counts only non-empty replay lines."""
    recorder = RunRecorder(run_id="run-3", sdd_dir=tmp_path)
    recorder.path.write_text('{"event": "one"}\n\n{"event": "two"}\n', encoding="utf-8")

    assert recorder.event_count() == 2


def test_load_replay_events_skips_malformed_lines(tmp_path: Path) -> None:
    """load_replay_events loads valid JSON lines and skips malformed ones."""
    replay_path = tmp_path / "replay.jsonl"
    replay_path.write_text('{"event":"one"}\nnot-json\n{"event":"two"}\n', encoding="utf-8")

    events = load_replay_events(replay_path)

    assert [event["event"] for event in events] == ["one", "two"]


def test_compute_replay_fingerprint_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """compute_replay_fingerprint returns an empty string when the replay file is absent."""
    assert compute_replay_fingerprint(tmp_path / "missing.jsonl") == ""
