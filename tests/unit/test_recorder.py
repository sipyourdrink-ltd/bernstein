"""Tests for the replay-log readers retained after the journal consolidation.

The canonical per-run recorder is now
:class:`bernstein.core.replay.journal.EventJournal` (issue #2293); the
old ``RunRecorder`` was removed. These tests cover the format-agnostic
JSONL readers the ``bernstein replay`` CLI still relies on.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.recorder import (
    compute_replay_fingerprint,
    load_replay_events,
)


def test_load_replay_events_skips_malformed_lines(tmp_path: Path) -> None:
    """load_replay_events loads valid JSON lines and skips malformed ones."""
    replay_path = tmp_path / "journal.jsonl"
    replay_path.write_text('{"event":"one"}\nnot-json\n{"event":"two"}\n', encoding="utf-8")

    events = load_replay_events(replay_path)

    assert [event["event"] for event in events] == ["one", "two"]


def test_compute_replay_fingerprint_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """compute_replay_fingerprint returns an empty string when the file is absent."""
    assert compute_replay_fingerprint(tmp_path / "missing.jsonl") == ""


def test_compute_replay_fingerprint_excludes_timing_envelope(tmp_path: Path) -> None:
    """Two logs differing only in ts/elapsed_s fingerprint identically (#1851)."""
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text('{"ts":1.0,"elapsed_s":0.1,"event":"x","v":1}\n', encoding="utf-8")
    b.write_text('{"ts":9.0,"elapsed_s":9.9,"event":"x","v":1}\n', encoding="utf-8")

    assert compute_replay_fingerprint(a) == compute_replay_fingerprint(b)
