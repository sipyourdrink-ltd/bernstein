"""TUI-019: Session recording and playback.

Records TUI screen state changes to a JSONL file so completed runs
can be replayed as a terminal movie. Useful for team reviews and demos.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
