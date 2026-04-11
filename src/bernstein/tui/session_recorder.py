"""TUI-019: Session recording and playback.

Records TUI screen state changes to a JSONL file so completed runs
can be replayed as a terminal movie. Useful for team reviews and demos.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingFrame:
    """A single recorded screen state frame.

    Attributes:
        timestamp: Seconds since recording started.
        event_type: Type of state change (e.g. "status_update", "task_transition").
        data: Snapshot of relevant state at this point.
    """

    timestamp: float
    event_type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class RecordingSummary:
    """Compact metadata summary for a saved TUI recording."""

    path: Path
    modified_ts: float
    frame_count: int
    duration_s: float
    last_event_type: str


class SessionRecorder:
    """Records TUI state changes to a JSONL file.

    Args:
        output_path: Path to the JSONL recording file.
    """

    def __init__(self, output_path: Path) -> None:
        self._path = output_path
        self._recording = False
        self._fh: Any = None

    @property
    def recording(self) -> bool:
        """Whether recording is active."""
        return self._recording

    def start(self) -> None:
        """Start recording. Creates/truncates the output file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._recording = True
        logger.info("Session recording started: %s", self._path)

    def stop(self) -> None:
        """Stop recording and close the file."""
        self._recording = False
        if self._fh:
            self._fh.close()
            self._fh = None
        logger.info("Session recording stopped: %s", self._path)

    def record_frame(
        self,
        timestamp: float,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Record a single frame.

        Args:
            timestamp: Seconds since recording started.
            event_type: Event type string.
            data: State snapshot dict.
        """
        if not self._recording or not self._fh:
            return

        frame = {
            "timestamp": timestamp,
            "event_type": event_type,
            "data": data,
        }
        self._fh.write(json.dumps(frame, separators=(",", ":")) + "\n")
        self._fh.flush()


class SessionPlayer:
    """Loads and iterates recorded session frames.

    Args:
        recording_path: Path to the JSONL recording file.
    """

    def __init__(self, recording_path: Path) -> None:
        self._path = recording_path

    def load_frames(self) -> list[RecordingFrame]:
        """Load all frames from the recording file.

        Returns:
            List of RecordingFrame in chronological order.
        """
        if not self._path.exists():
            return []

        frames: list[RecordingFrame] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                frames.append(
                    RecordingFrame(
                        timestamp=float(raw["timestamp"]),
                        event_type=str(raw["event_type"]),
                        data=dict(raw.get("data", {})),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Skipping malformed recording frame")
        return frames


def summarize_recording(recording_path: Path) -> RecordingSummary | None:
    """Build a compact summary for a recording JSONL file."""
    player = SessionPlayer(recording_path)
    frames = player.load_frames()
    if not frames:
        return None
    try:
        modified_ts = recording_path.stat().st_mtime
    except OSError:
        modified_ts = 0.0
    return RecordingSummary(
        path=recording_path,
        modified_ts=modified_ts,
        frame_count=len(frames),
        duration_s=max(0.0, frames[-1].timestamp - frames[0].timestamp),
        last_event_type=frames[-1].event_type,
    )


def list_recordings(recordings_dir: Path, limit: int = 5) -> list[RecordingSummary]:
    """List recent recordings with lightweight summaries, newest first."""
    if not recordings_dir.exists():
        return []
    summaries = [
        summary
        for path in recordings_dir.glob("*.jsonl")
        if (summary := summarize_recording(path)) is not None
    ]
    summaries.sort(key=lambda summary: summary.modified_ts, reverse=True)
    return summaries[:limit]


def render_session_recorder_panel(
    *,
    recording_active: bool,
    active_recording: Path | None,
    recordings: list[RecordingSummary],
    selected_recording: Path | None = None,
    playback_frame: RecordingFrame | None = None,
) -> Text:
    """Render recorder state, recent recordings, and current playback preview."""
    text = Text()
    if recording_active:
        text.append("Recorder ", style="bold")
        text.append("REC", style="bold green")
    else:
        text.append("Recorder ", style="bold")
        text.append("idle", style="dim")
    text.append("  [Ctrl+P: record/replay]", style="dim")

    if active_recording is not None:
        text.append("\n")
        text.append(f"Writing: {active_recording.name}", style="cyan")

    text.append("\n")
    text.append("Recent recordings", style="bold")
    if not recordings:
        text.append("\n")
        text.append("No recordings yet.", style="dim")
    else:
        for summary in recordings:
            marker = "> " if selected_recording == summary.path else "  "
            stamp = datetime.fromtimestamp(summary.modified_ts).strftime("%H:%M:%S")
            text.append("\n")
            text.append(marker, style="bold cyan" if marker.strip() else "dim")
            text.append(
                f"{stamp} {summary.path.stem} ({summary.frame_count} frames, {summary.duration_s:.1f}s)",
                style="white",
            )
            text.append(f" {summary.last_event_type}", style="dim")

    if playback_frame is not None:
        text.append("\n\n")
        text.append("Playback", style="bold")
        text.append("\n")
        text.append(f"{playback_frame.timestamp:.1f}s ", style="dim")
        text.append(playback_frame.event_type, style="yellow")
        summary = playback_frame.data.get("summary")
        if isinstance(summary, dict):
            done = int(summary.get("done", 0))
            total = int(summary.get("total", 0))
            text.append(f"  tasks {done}/{total}", style="dim")
    return text


class SessionRecorderPanel(Static):
    """Compact side panel for recording state and replay preview."""

    DEFAULT_CSS = """
    SessionRecorderPanel {
        height: auto;
        max-height: 12;
        border-top: solid #333;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise with an empty recorder snapshot."""
        super().__init__(**kwargs)
        self._recording_active = False
        self._active_recording: Path | None = None
        self._recordings: list[RecordingSummary] = []
        self._selected_recording: Path | None = None
        self._playback_frame: RecordingFrame | None = None

    def set_snapshot(
        self,
        *,
        recording_active: bool,
        active_recording: Path | None,
        recordings: list[RecordingSummary],
        selected_recording: Path | None = None,
        playback_frame: RecordingFrame | None = None,
    ) -> None:
        """Update the rendered recorder snapshot."""
        self._recording_active = recording_active
        self._active_recording = active_recording
        self._recordings = recordings
        self._selected_recording = selected_recording
        self._playback_frame = playback_frame
        self.refresh()

    def render(self) -> Text:
        """Render the current recorder snapshot."""
        return render_session_recorder_panel(
            recording_active=self._recording_active,
            active_recording=self._active_recording,
            recordings=self._recordings,
            selected_recording=self._selected_recording,
            playback_frame=self._playback_frame,
        )
