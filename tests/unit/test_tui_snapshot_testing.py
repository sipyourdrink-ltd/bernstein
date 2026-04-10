"""Tests for TUI visual regression snapshot testing (test-019)."""

from __future__ import annotations

from pathlib import Path

from bernstein.tui.snapshot_testing import (
    SnapshotConfig,
    SnapshotResult,
    capture_widget_text,
    compare_snapshot,
    format_snapshot_diff,
    list_snapshots,
    update_snapshot,
)

# ---------------------------------------------------------------------------
# SnapshotConfig defaults
# ---------------------------------------------------------------------------


class TestSnapshotConfig:
    def test_defaults(self) -> None:
        cfg = SnapshotConfig()
        assert cfg.snapshot_dir == ".sdd/tui_snapshots"
        assert cfg.update_mode is False
        assert cfg.terminal_size == (120, 40)

    def test_custom_values(self) -> None:
        cfg = SnapshotConfig(
            snapshot_dir="/tmp/snaps",
            update_mode=True,
            terminal_size=(80, 24),
        )
        assert cfg.snapshot_dir == "/tmp/snaps"
        assert cfg.update_mode is True
        assert cfg.terminal_size == (80, 24)

    def test_frozen(self) -> None:
        cfg = SnapshotConfig()
        try:
            cfg.update_mode = True  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised, "SnapshotConfig should be frozen"


# ---------------------------------------------------------------------------
# SnapshotResult
# ---------------------------------------------------------------------------


class TestSnapshotResult:
    def test_defaults(self) -> None:
        result = SnapshotResult(widget_name="w", matched=True)
        assert result.diff_lines == []
        assert result.snapshot_path == ""

    def test_frozen(self) -> None:
        result = SnapshotResult(widget_name="w", matched=True)
        try:
            result.matched = False  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised, "SnapshotResult should be frozen"


# ---------------------------------------------------------------------------
# capture_widget_text
# ---------------------------------------------------------------------------


class TestCaptureWidgetText:
    def test_strips_ansi_codes(self) -> None:
        raw = "\x1b[31mError\x1b[0m: something broke"
        result = capture_widget_text("StatusBar", raw)
        assert "\x1b" not in result
        assert "Error: something broke" in result

    def test_normalizes_whitespace(self) -> None:
        raw = "col1    col2\t\tcol3   "
        result = capture_widget_text("Table", raw)
        assert result == "col1 col2 col3"

    def test_preserves_lines(self) -> None:
        raw = "line1\nline2\nline3"
        result = capture_widget_text("Widget", raw)
        assert result.splitlines() == ["line1", "line2", "line3"]

    def test_combined_ansi_and_whitespace(self) -> None:
        raw = "\x1b[1m  hello \x1b[0m   world  "
        result = capture_widget_text("TestWidget", raw)
        assert result == " hello world"

    def test_empty_input(self) -> None:
        result = capture_widget_text("Empty", "")
        assert result == ""

    def test_multiline_ansi(self) -> None:
        raw = "\x1b[32mOK\x1b[0m\n\x1b[31mFAIL\x1b[0m"
        result = capture_widget_text("Status", raw)
        assert result == "OK\nFAIL"


# ---------------------------------------------------------------------------
# compare_snapshot — new snapshot
# ---------------------------------------------------------------------------


class TestCompareSnapshotNew:
    def test_creates_baseline_on_first_run(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        result = compare_snapshot("widget_a", "hello world", snap_dir)

        assert result.matched is True
        assert result.widget_name == "widget_a"
        assert result.diff_lines == []
        assert (snap_dir / "widget_a.snap").exists()
        assert (snap_dir / "widget_a.snap").read_text() == "hello world"

    def test_snapshot_path_populated(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        result = compare_snapshot("my_widget", "data", snap_dir)
        assert result.snapshot_path == str(snap_dir / "my_widget.snap")


# ---------------------------------------------------------------------------
# compare_snapshot — match / mismatch
# ---------------------------------------------------------------------------


class TestCompareSnapshotMatch:
    def test_match_returns_true(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        update_snapshot("w", "content", snap_dir)

        result = compare_snapshot("w", "content", snap_dir)
        assert result.matched is True
        assert result.diff_lines == []

    def test_mismatch_detected(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        update_snapshot("w", "old text", snap_dir)

        result = compare_snapshot("w", "new text", snap_dir)
        assert result.matched is False
        assert len(result.diff_lines) > 0

    def test_mismatch_diff_contains_changes(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        update_snapshot("w", "alpha\nbeta\n", snap_dir)

        result = compare_snapshot("w", "alpha\ngamma\n", snap_dir)
        assert result.matched is False
        diff_text = "".join(result.diff_lines)
        assert "beta" in diff_text
        assert "gamma" in diff_text


# ---------------------------------------------------------------------------
# update_snapshot
# ---------------------------------------------------------------------------


class TestUpdateSnapshot:
    def test_creates_directory_and_file(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "deep" / "nested" / "snaps"
        path = update_snapshot("component", "rendered text", snap_dir)

        assert path.exists()
        assert path.read_text() == "rendered text"
        assert path.name == "component.snap"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        update_snapshot("w", "v1", snap_dir)
        path = update_snapshot("w", "v2", snap_dir)

        assert path.read_text() == "v2"

    def test_returns_correct_path(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        path = update_snapshot("my_widget", "text", snap_dir)
        assert path == snap_dir / "my_widget.snap"


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------


class TestListSnapshots:
    def test_empty_when_dir_missing(self, tmp_path: Path) -> None:
        result = list_snapshots(tmp_path / "nonexistent")
        assert result == []

    def test_lists_snapshot_names_sorted(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        update_snapshot("zebra", "z", snap_dir)
        update_snapshot("alpha", "a", snap_dir)
        update_snapshot("middle", "m", snap_dir)

        names = list_snapshots(snap_dir)
        assert names == ["alpha", "middle", "zebra"]

    def test_ignores_non_snap_files(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        snap_dir.mkdir()
        (snap_dir / "readme.txt").write_text("ignore me")
        update_snapshot("widget", "data", snap_dir)

        names = list_snapshots(snap_dir)
        assert names == ["widget"]

    def test_empty_directory(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snaps"
        snap_dir.mkdir()

        names = list_snapshots(snap_dir)
        assert names == []


# ---------------------------------------------------------------------------
# format_snapshot_diff
# ---------------------------------------------------------------------------


class TestFormatSnapshotDiff:
    def test_match_message(self) -> None:
        result = SnapshotResult(
            widget_name="StatusBar",
            matched=True,
        )
        output = format_snapshot_diff(result)
        assert "[OK]" in output
        assert "StatusBar" in output
        assert "matches" in output

    def test_mismatch_message(self) -> None:
        result = SnapshotResult(
            widget_name="TaskList",
            matched=False,
            diff_lines=["--- a\n", "+++ b\n", "-old\n", "+new\n"],
        )
        output = format_snapshot_diff(result)
        assert "[MISMATCH]" in output
        assert "TaskList" in output
        assert "-old" in output
        assert "+new" in output

    def test_empty_diff_lines_on_mismatch(self) -> None:
        result = SnapshotResult(
            widget_name="W",
            matched=False,
            diff_lines=[],
        )
        output = format_snapshot_diff(result)
        assert "[MISMATCH]" in output
