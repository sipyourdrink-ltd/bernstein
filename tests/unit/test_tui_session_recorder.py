"""Tests for TUI-019: Session recording and playback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.tui.session_recorder import (
    RecordingFrame,
    SessionPlayer,
    SessionRecorder,
    list_recordings,
    render_session_recorder_panel,
    summarize_recording,
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
        assert frame["timestamp"] == pytest.approx(1000.0)
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
        assert frames[1].timestamp == pytest.approx(1.0)

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
        assert frame.timestamp == pytest.approx(1.0)
        assert frame.data == {"k": "v"}


class TestRecordingSummaries:
    def test_summarize_recording(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(timestamp=0.0, event_type="a", data={})
        recorder.record_frame(timestamp=2.5, event_type="b", data={})
        recorder.stop()

        summary = summarize_recording(path)

        assert summary is not None
        assert summary.frame_count == 2
        assert summary.duration_s == pytest.approx(2.5)
        assert summary.last_event_type == "b"

    def test_list_recordings_newest_first(self, tmp_path: Path) -> None:
        older = tmp_path / "older.jsonl"
        newer = tmp_path / "newer.jsonl"
        for index, path in enumerate((older, newer), start=1):
            recorder = SessionRecorder(path)
            recorder.start()
            recorder.record_frame(timestamp=float(index), event_type=f"event-{index}", data={})
            recorder.stop()

        newer.touch()

        recordings = list_recordings(tmp_path)

        assert [recording.path.name for recording in recordings] == ["newer.jsonl", "older.jsonl"]


class TestRenderSessionRecorderPanel:
    def test_empty_state(self) -> None:
        rendered = render_session_recorder_panel(recording_active=False, active_recording=None, recordings=[])
        plain = rendered.plain

        assert "Recorder idle" in plain
        assert "No recordings yet." in plain

    def test_playback_preview(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(timestamp=1.0, event_type="status_update", data={"summary": {"done": 2, "total": 5}})
        recorder.stop()
        summary = summarize_recording(path)
        assert summary is not None

        rendered = render_session_recorder_panel(
            recording_active=True,
            active_recording=path,
            recordings=[summary],
            selected_recording=path,
            playback_frame=RecordingFrame(
                timestamp=1.0,
                event_type="status_update",
                data={"summary": {"done": 2, "total": 5}},
            ),
        )
        plain = rendered.plain

        assert "Recorder REC" in plain
        assert "Playback" in plain
        assert "tasks 2/5" in plain
