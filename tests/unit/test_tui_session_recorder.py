"""Tests for TUI-019: Session recording and playback."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.tui.session_recorder import (
    RecordingFrame,
    SessionPlayer,
    SessionRecorder,
)


class TestSessionRecorder:
    def test_record_frame(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(
            timestamp=1000.0,
            event_type="status_update",
            data={"agents": 2, "tasks_done": 5},
        )
        recorder.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        frame = json.loads(lines[0])
        assert frame["timestamp"] == 1000.0
        assert frame["event_type"] == "status_update"
        assert frame["data"]["agents"] == 2

    def test_multiple_frames(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        for i in range(5):
            recorder.record_frame(
                timestamp=float(i),
                event_type="tick",
                data={"i": i},
            )
        recorder.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_not_recording_before_start(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.record_frame(timestamp=0.0, event_type="x", data={})
        assert not path.exists()

    def test_not_recording_after_stop(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(timestamp=0.0, event_type="a", data={})
        recorder.stop()
        recorder.record_frame(timestamp=1.0, event_type="b", data={})

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1


class TestSessionPlayer:
    def test_load_frames(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(timestamp=0.0, event_type="a", data={"x": 1})
        recorder.record_frame(timestamp=1.0, event_type="b", data={"x": 2})
        recorder.stop()

        player = SessionPlayer(path)
        frames = player.load_frames()
        assert len(frames) == 2
        assert frames[0].event_type == "a"
        assert frames[1].timestamp == 1.0

    def test_load_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        player = SessionPlayer(path)
        assert player.load_frames() == []

    def test_load_missing_file(self, tmp_path: Path) -> None:
        player = SessionPlayer(tmp_path / "nope.jsonl")
        assert player.load_frames() == []

    def test_frame_dataclass(self) -> None:
        frame = RecordingFrame(timestamp=1.0, event_type="test", data={"k": "v"})
        assert frame.timestamp == 1.0
        assert frame.data == {"k": "v"}
