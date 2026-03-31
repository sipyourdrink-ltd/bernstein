"""Tests for FrameBuffer double-buffered renderer (GFX-07)."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.cli.frame_buffer import FrameBuffer

SYNC_BEGIN = "\033[?2026h"
SYNC_END = "\033[?2026l"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


class FakeStream:
    """Minimal write-capturing stream for assertions."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass

    @property
    def combined(self) -> str:
        return "".join(self.writes)


# ── Construction ───────────────────────────────────────────────────────────


def test_default_fps_is_30() -> None:
    buf = FrameBuffer()
    assert buf.fps == 30


def test_custom_fps_is_stored() -> None:
    buf = FrameBuffer(fps=60)
    assert buf.fps == 60


# ── Synchronized output markers ────────────────────────────────────────────


def test_render_frame_emits_sync_begin_marker() -> None:
    """Every rendered frame must begin with Mode 2026 begin marker."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO")
    assert SYNC_BEGIN in stream.combined


def test_render_frame_emits_sync_end_marker() -> None:
    """Every rendered frame must end with Mode 2026 end marker."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO")
    assert SYNC_END in stream.combined


def test_sync_begin_precedes_sync_end() -> None:
    """Begin marker must appear before end marker in output."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO")
    assert stream.combined.index(SYNC_BEGIN) < stream.combined.index(SYNC_END)


# ── Double buffering: single write call ────────────────────────────────────


def test_render_frame_single_write_call() -> None:
    """render_frame must flush all output in exactly one stream.write() call."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO\nWORLD")
    assert len(stream.writes) == 1


def test_render_frame_second_changed_frame_single_write_call() -> None:
    """A changed second frame must also use exactly one write() call."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO")
    stream.writes.clear()
    buf.render_frame("WORLD")
    assert len(stream.writes) == 1


# ── First frame writes full content ────────────────────────────────────────


def test_first_frame_writes_full_content() -> None:
    """First render must include all frame content in output."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO\nWORLD")
    assert "HELLO" in stream.combined
    assert "WORLD" in stream.combined


# ── Dirty-rect ─────────────────────────────────────────────────────────────


def test_dirty_rect_unchanged_frame_writes_nothing() -> None:
    """Identical second frame must produce zero stream writes."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO\nWORLD")
    stream.writes.clear()
    buf.render_frame("HELLO\nWORLD")
    assert len(stream.writes) == 0


def test_dirty_rect_changed_row_omits_unchanged_row() -> None:
    """Second frame with one changed row must not re-emit the unchanged row."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO\nWORLD")
    stream.writes.clear()
    buf.render_frame("HELLO\nWORXX")  # row 0 unchanged, row 1 partially changed
    assert "HELLO" not in stream.combined
    assert "XX" in stream.combined


def test_dirty_rect_changed_row_omits_unchanged_prefix() -> None:
    """Dirty-rect must skip the unchanged prefix within a changed row."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO\nWORLD")
    stream.writes.clear()
    buf.render_frame("HELLO\nWORXX")
    # "WOR" is the shared prefix — it must not appear in the delta
    assert "WOR" not in stream.combined


def test_dirty_rect_all_rows_changed_emits_all() -> None:
    """When every row changes, all content must be present in second render."""
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.render_frame("HELLO\nWORLD")
    stream.writes.clear()
    buf.render_frame("XXXXX\nYYYYY")
    assert "XXXXX" in stream.combined
    assert "YYYYY" in stream.combined


# ── Cursor management ──────────────────────────────────────────────────────


def test_hide_cursor_emits_escape_sequence() -> None:
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.hide_cursor()
    assert HIDE_CURSOR in stream.combined


def test_show_cursor_emits_escape_sequence() -> None:
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    buf.show_cursor()
    assert SHOW_CURSOR in stream.combined


def test_context_manager_hides_cursor_on_enter() -> None:
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    with buf:
        assert HIDE_CURSOR in stream.combined


def test_context_manager_shows_cursor_on_exit() -> None:
    stream = FakeStream()
    buf = FrameBuffer(fps=10_000, stream=stream)
    with buf:
        stream.writes.clear()
    assert SHOW_CURSOR in stream.combined


# ── FPS limiter ─────────────────────────────────────────────────────────────


def test_fps_limiter_sleeps_between_rapid_frames() -> None:
    """render_frame must sleep to respect target fps when called too quickly."""
    stream = FakeStream()
    buf = FrameBuffer(fps=1, stream=stream)  # 1 fps → 1 s interval
    buf.render_frame("first")
    with patch("bernstein.cli.frame_buffer.time") as mock_time:
        # monotonic returns a value very close to last frame time
        mock_time.monotonic.return_value = buf._last_frame_time + 0.001
        buf.render_frame("second")
        mock_time.sleep.assert_called_once()


def test_fps_limiter_does_not_sleep_after_long_gap() -> None:
    """render_frame must not sleep when enough time has already elapsed."""
    stream = FakeStream()
    buf = FrameBuffer(fps=1, stream=stream)  # 1 fps → 1 s interval
    buf.render_frame("first")
    with patch("bernstein.cli.frame_buffer.time") as mock_time:
        # monotonic returns a value 2 s after last frame — no sleep needed
        mock_time.monotonic.return_value = buf._last_frame_time + 2.0
        buf.render_frame("second")
        mock_time.sleep.assert_not_called()
