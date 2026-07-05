"""Tests for refused-vs-failed separation in status surfaces (#2244).

``bernstein status`` and the dashboard/fleet status payloads must report
refused tasks as their own bucket, never folded into failed.
"""

from __future__ import annotations

from bernstein.cli.ui import STATUS_COLORS, RunStats, TaskSummary, create_summary_plain, create_summary_table
from bernstein.tui.task_list import STATUS_COLORS as TUI_STATUS_COLORS
from bernstein.tui.task_list import STATUS_DOTS, status_color


class TestTaskSummary:
    def test_from_dict_reads_refused(self) -> None:
        summary = TaskSummary.from_dict({"total": 5, "done": 2, "failed": 1, "refused": 2})
        assert summary.refused == 2
        assert summary.failed == 1

    def test_refused_defaults_to_zero(self) -> None:
        summary = TaskSummary.from_dict({"total": 1, "done": 1})
        assert summary.refused == 0


class TestSummaryRendering:
    def test_plain_summary_lists_refused_separately(self) -> None:
        stats = RunStats(summary=TaskSummary(total=4, done=1, failed=1, refused=2))
        plain = create_summary_plain(stats)
        assert "Refused:     2" in plain
        assert "Failed:      1" in plain

    def test_summary_table_has_refused_row(self) -> None:
        stats = RunStats(summary=TaskSummary(total=4, done=1, failed=1, refused=2))
        table = create_summary_table(stats)
        row_labels = [str(col) for col in table.columns[0]._cells]  # type: ignore[attr-defined]
        assert any("Refused" in label for label in row_labels)


class TestStatusColors:
    def test_cli_status_color_registered(self) -> None:
        assert STATUS_COLORS["refused"] != STATUS_COLORS["failed"]

    def test_tui_status_color_registered(self) -> None:
        assert "refused" in TUI_STATUS_COLORS
        assert TUI_STATUS_COLORS["refused"] != TUI_STATUS_COLORS["failed"]
        assert status_color("refused") == TUI_STATUS_COLORS["refused"]

    def test_tui_status_dot_registered(self) -> None:
        assert "refused" in STATUS_DOTS
