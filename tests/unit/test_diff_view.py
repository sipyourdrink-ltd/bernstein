"""Tests for enhanced diff view."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.cli.diff_cmd import FileDiffStat, ResolvedDiff, _render_enhanced_summary


def test_render_enhanced_summary() -> None:
    """Test that enhanced summary rendering doesn't crash and computes totals."""
    stats = [
        FileDiffStat(path="src/main.py", additions=10, deletions=5),
        FileDiffStat(path="tests/test_main.py", additions=2, deletions=10),  # MODERATE risk
        FileDiffStat(path="config/secrets.yaml", additions=1, deletions=0),  # HIGH risk
    ]
    resolved = ResolvedDiff(diff_text="some diff", source_label="source", file_stats=stats)

    with patch("bernstein.cli.diff_cmd.console.print") as mock_print:
        _render_enhanced_summary(resolved)
        assert mock_print.called

        # Find the call that passed a Panel
        panel = None
        for call in mock_print.call_args_list:
            if call.args and hasattr(call.args[0], "subtitle"):
                panel = call.args[0]
                break

        assert panel is not None
        assert "Total: [green]+13[/green] [red]-15[/red]" in str(panel.subtitle)
