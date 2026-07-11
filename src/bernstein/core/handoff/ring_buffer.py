"""Bounded log ring buffer used to replay the stream tail on handoff.

When a surface (terminal, chat, dashboard) hands off, the destination
needs to render the last few hundred lines of agent output so the
operator does not see a blank pane while the live stream catches up.
We keep that tail on disk per session so any surface can read it back
without coordinating with the source process.

Storage layout::

    .sdd/runtime/handoff_tail/<session_id>.jsonl

Each line is a JSON object with ``ts`` (epoch seconds), ``surface``
(``terminal``/``chat``/``dashboard``), and ``text`` (the raw line that
was streamed to the user). Writers call :meth:`StreamTailBuffer.append`
which trims older lines once the file crosses
``max_entries`` records - this keeps replay cheap and bounds the on-disk
footprint regardless of run length.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "StreamTailBuffer",
    "TailEntry",
]

DEFAULT_MAX_ENTRIES: Final[int] = 500
_TAIL_DIR: Final[Path] = Path(".sdd") / "runtime" / "handoff_tail"


@dataclass(slots=True, frozen=True)
class TailEntry:
    """One captured line of agent output preserved for replay.

    Attributes:
        ts: Epoch seconds when the line was emitted.
        surface: Origin surface - ``"terminal"``, ``"chat"`` or
            ``"dashboard"``.
        text: Raw line as it was rendered to the operator. Trailing
            newlines are stripped on append so the wire format stays
            line-oriented.
    """

    ts: float
    surface: str
    text: str

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return {"ts": self.ts, "surface": self.surface, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TailEntry:
        """Deserialise from a JSON-parsed dict.

        Unknown keys are ignored; missing fields fall back to safe
        defaults so a single torn line does not break the replay.

        Args:
            data: Parsed JSON object.

        Returns:
            Populated :class:`TailEntry`.
        """
        ts_raw = data.get("ts", 0.0)
        try:
            ts = float(ts_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ts = 0.0
        return cls(
            ts=ts,
            surface=str(data.get("surface", "")),
            text=str(data.get("text", "")),
        )


class StreamTailBuffer:
    """File-backed ring buffer for one session's recent stream output.

    The buffer is intentionally simple - append-only JSONL with a hard
    cap on the line count. We trim by rewriting the tail when the line
    count would exceed ``max_entries``; this is O(N) on trim but the
    cap keeps N small (default 500 lines).

    The class is process-safe but not thread-safe; callers that share
    a buffer between threads should serialise their own access.
    """

    def __init__(
        self,
        workdir: Path,
        session_id: str,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        """Create or open a buffer for ``session_id``.

        Args:
            workdir: Project root (the ``.sdd/`` parent).
            session_id: Bernstein session whose tail we are recording.
            max_entries: Hard cap on lines retained on disk.
        """
        if not session_id:
            raise ValueError("session_id is required")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._workdir = workdir
        self._session_id = session_id
        self._max_entries = max_entries
        # The session id becomes a filename component; normalise the
        # candidate and require it to stay inside the tail directory so a
        # hostile id ("../", absolute path) cannot address other files.
        tail_dir = os.path.realpath(workdir / _TAIL_DIR)
        candidate = os.path.realpath(os.path.join(tail_dir, f"{session_id}.jsonl"))
        if not candidate.startswith(tail_dir + os.sep):
            raise ValueError("session_id must resolve inside the handoff tail directory")
        self._path = Path(candidate)

    @property
    def path(self) -> Path:
        """Absolute path of the on-disk JSONL file."""
        return self._path

    def append(self, surface: str, text: str, *, ts: float | None = None) -> None:
        """Record one line of streamed output.

        Trims older lines once the on-disk count exceeds
        ``max_entries`` so the file never grows unbounded.

        Args:
            surface: Origin surface (``"terminal"`` / ``"chat"`` /
                ``"dashboard"``).
            text: Raw stream line. A single trailing newline is stripped
                so the JSONL stays well-formed.
            ts: Optional explicit timestamp; defaults to ``time.time()``.
        """
        entry = TailEntry(
            ts=ts if ts is not None else time.time(),
            surface=surface,
            text=text.rstrip("\n"),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict()) + "\n")
        self._maybe_trim()

    def read(self, *, limit: int | None = None) -> list[TailEntry]:
        """Return the buffered tail in chronological order.

        Args:
            limit: If set, return only the last ``limit`` entries.

        Returns:
            Ordered list of :class:`TailEntry`. Empty when the buffer
            file is missing or unreadable.
        """
        if not self._path.exists():
            return []
        entries: list[TailEntry] = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                entries.append(TailEntry.from_dict(parsed))  # type: ignore[arg-type]
        except OSError:
            return []
        if limit is not None and limit >= 0:
            return entries[-limit:]
        return entries

    def clear(self) -> None:
        """Delete the on-disk buffer (no-op if missing)."""
        self._path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _maybe_trim(self) -> None:
        """Rewrite the file if it has grown past ``max_entries``.

        Reads the file content once inside a ``with`` block so the FD is
        closed on every code path; the previous ``sum(1 for _ in
        path.open(...))`` form leaked the handle until GC ran, and in
        tight append loops the leak surfaced as ``OSError: Too many
        open files``. The temp filename includes the current PID so two
        writers cannot stomp each other's ``.tmp`` mid-rename.
        """
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return
        if len(lines) <= self._max_entries:
            return
        keep = lines[-self._max_entries :]
        tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
        try:
            tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            tmp.unlink(missing_ok=True)
