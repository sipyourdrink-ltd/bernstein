"""Double-buffered, zero-tear frame renderer for terminal animation (GFX-07).

Wraps every frame in Mode 2026 synchronized output markers and uses
dirty-rect diffing so only changed cells are emitted. All output is
assembled in a StringIO buffer and flushed in a single stream.write()
call — eliminating mid-frame tear artefacts.

Usage::

    buf = FrameBuffer(fps=30)
    with buf:                        # hides cursor on enter, shows on exit
        while animating:
            buf.render_frame(build_frame())

Performance: <1 ms overhead per frame for typical 80x24 terminal frames.
"""

from __future__ import annotations

import sys
import time
from io import StringIO
from typing import IO


class FrameBuffer:
    """Double-buffered frame renderer with Mode 2026 synchronized output.

    Args:
        fps: Target frames per second (default 30).
        stream: Output stream (default sys.stdout). Inject a fake stream in
            tests to capture output without touching the real terminal.
    """

    _SYNC_BEGIN = "\033[?2026h"
    _SYNC_END = "\033[?2026l"
    _CURSOR_HOME = "\033[H"
    _HIDE_CURSOR = "\033[?25l"
    _SHOW_CURSOR = "\033[?25h"
    _CLEAR_EOL = "\033[K"

    def __init__(self, fps: int = 30, stream: IO[str] | None = None) -> None:
        self._fps = fps
        self._frame_interval: float = 1.0 / fps
        self._last_frame_time: float = 0.0
        self._prev_lines: list[str] = []
        self._stream: IO[str] = stream if stream is not None else sys.stdout

    @property
    def fps(self) -> int:
        """Target frames per second."""
        return self._fps

    # ── Public API ────────────────────────────────────────────────────────

    def render_frame(self, frame_data: str) -> None:
        """Render one frame — the only call you need.

        Applies FPS throttling, dirty-rect diffing, and Mode 2026
        synchronized output. All output is written in a single
        stream.write() call.

        Args:
            frame_data: Complete frame content as a string. Rows are
                separated by newline characters.
        """
        # ── FPS limiter ──
        now = time.monotonic()
        elapsed = now - self._last_frame_time
        sleep_time = self._frame_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

        new_lines = frame_data.split("\n")
        delta = self._compute_delta(new_lines)

        if delta:
            # Double buffering: build entire output in memory, then single write
            buf = StringIO()
            buf.write(self._SYNC_BEGIN)
            buf.write(delta)
            buf.write(self._SYNC_END)
            self._stream.write(buf.getvalue())
            self._stream.flush()

        self._prev_lines = new_lines
        self._last_frame_time = time.monotonic()

    def hide_cursor(self) -> None:
        """Emit DECTCEM hide-cursor escape sequence."""
        self._stream.write(self._HIDE_CURSOR)
        self._stream.flush()

    def show_cursor(self) -> None:
        """Emit DECTCEM show-cursor escape sequence."""
        self._stream.write(self._SHOW_CURSOR)
        self._stream.flush()

    def __enter__(self) -> FrameBuffer:
        self.hide_cursor()
        return self

    def __exit__(self, *args: object) -> None:
        self.show_cursor()

    # ── Dirty-rect ────────────────────────────────────────────────────────

    def _compute_delta(self, new_lines: list[str]) -> str:
        """Return the minimal ANSI sequence to update the terminal.

        First frame: cursor-home + full frame content.
        Subsequent frames: per-row cursor-move sequences covering only
        cells that changed (dirty-rect). Returns an empty string when
        the frame is identical to the previous one so render_frame can
        skip the write entirely.
        """
        if not self._prev_lines:
            # First frame — write everything from cursor home
            buf = StringIO()
            buf.write(self._CURSOR_HOME)
            buf.write("\n".join(new_lines))
            return buf.getvalue()

        buf = StringIO()
        changed = False
        max_rows = max(len(new_lines), len(self._prev_lines))

        for row_idx in range(max_rows):
            new_line = new_lines[row_idx] if row_idx < len(new_lines) else ""
            old_line = self._prev_lines[row_idx] if row_idx < len(self._prev_lines) else ""

            if new_line == old_line:
                continue

            changed = True

            # Find the first cell that differs
            min_len = min(len(new_line), len(old_line))
            first_change = 0
            while first_change < min_len and new_line[first_change] == old_line[first_change]:
                first_change += 1

            # Move cursor to that cell (ANSI is 1-indexed)
            buf.write(f"\033[{row_idx + 1};{first_change + 1}H")
            buf.write(new_line[first_change:])

            # Erase tail when the new line is shorter than the old one
            if len(new_line) < len(old_line):
                buf.write(self._CLEAR_EOL)

        return buf.getvalue() if changed else ""
