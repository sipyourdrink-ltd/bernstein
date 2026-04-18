"""Deterministic replay recorder for orchestration runs.

Records every significant event during an orchestration run to a JSONL file
at `.sdd/runs/{run_id}/replay.jsonl`. The replay log enables:
  - Post-hoc debugging: see exactly what each agent saw and produced.
  - Reproducibility proof: SHA-256 fingerprint of the full event stream.
  - `bernstein replay <run_id>`: step-by-step playback in the terminal.

Usage:
    recorder = RunRecorder(run_id="20240315-143022", sdd_dir=Path(".sdd"))
    recorder.record("task_claimed", task_id="T-001", agent_id="backend-abc", model="sonnet")
    recorder.record("agent_spawned", agent_id="backend-abc", prompt_hash="sha256:abc123")
    recorder.record("task_completed", task_id="T-001", files_modified=["src/auth.py"], cost_usd=0.12)
    fingerprint = recorder.fingerprint()
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import JANITOR
from bernstein.core.persistence.runtime_state import rotate_log_file

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class RunRecorder:
    """Append-only JSONL recorder for a single orchestration run.

    Thread-safe for single-writer usage (the orchestrator tick loop is
    single-threaded). File is opened/closed per write to avoid holding
    file handles across long tick intervals.

    Args:
        run_id: Unique identifier for the run (e.g. ``"20240315-143022"``).
        sdd_dir: Path to the ``.sdd`` directory.
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._run_id = run_id
        self._path = sdd_dir / "runs" / run_id / "replay.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._start_ts: float = time.time()

    @property
    def run_id(self) -> str:
        """The run identifier this recorder is writing to."""
        return self._run_id

    @property
    def path(self) -> Path:
        """Path to the replay JSONL file."""
        return self._path

    def record(self, event: str, **data: Any) -> None:
        """Append a single event to the replay log.

        Args:
            event: Event type (e.g. ``"task_claimed"``, ``"agent_spawned"``).
            **data: Arbitrary key-value pairs for the event payload.
        """
        entry: dict[str, Any] = {
            "ts": time.time(),
            "elapsed_s": round(time.time() - self._start_ts, 3),
            "event": event,
        }
        entry.update(data)
        # audit-081: cap unbounded replay.jsonl. `bernstein replay` may stitch
        # live + rotated backups if needed — see load_replay_events.
        rotate_log_file(self._path, max_bytes=JANITOR.replay_rotate_bytes)
        try:
            with self._path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning("RunRecorder: failed to write event %r: %s", event, exc)

    def fingerprint(self) -> str:
        """Compute SHA-256 fingerprint of the entire replay log.

        Returns:
            Hex-encoded SHA-256 hash, or empty string if the file doesn't exist.
        """
        if not self._path.exists():
            return ""
        sha = hashlib.sha256()
        try:
            with self._path.open("rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha.update(chunk)
        except OSError as exc:
            logger.warning("RunRecorder: failed to read replay log for fingerprint: %s", exc)
            return ""
        return sha.hexdigest()

    def event_count(self) -> int:
        """Return the number of events recorded so far."""
        if not self._path.exists():
            return 0
        try:
            with self._path.open() as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0


def load_replay_events(replay_path: Path) -> list[dict[str, Any]]:
    """Load all events from a replay JSONL file.

    Args:
        replay_path: Path to the ``replay.jsonl`` file.

    Returns:
        List of event dicts, ordered by timestamp.
    """
    events: list[dict[str, Any]] = []
    if not replay_path.exists():
        return events
    with replay_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def compute_replay_fingerprint(replay_path: Path) -> str:
    """Compute SHA-256 fingerprint of a replay log file.

    Args:
        replay_path: Path to the ``replay.jsonl`` file.

    Returns:
        Hex-encoded SHA-256 hash, or empty string if the file doesn't exist.
    """
    if not replay_path.exists():
        return ""
    sha = hashlib.sha256()
    with replay_path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()
