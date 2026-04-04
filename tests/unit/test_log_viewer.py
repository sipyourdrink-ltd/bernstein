"""Tests for log viewer with syntax highlighting, diff folding, and markdown rendering."""

from __future__ import annotations

from bernstein.tui.log_viewer import (
    DiffBlock,
    LogViewer,
    _looks_like_markdown,
    apply_diff_folding,
    detect_code_blocks,
    detect_diff_blocks,
    fold_diff_lines,
    has_code_blocks,
)


class TestLogViewer:
    """Test log viewer syntax highlighting."""

    def test_detect_code_blocks_present(self) -> None:
        """Test detecting code blocks."""
        log_text = """
Some text
```python
def hello():
    print("world")
```
More text
"""
        blocks = detect_code_blocks(log_text)

        assert len(blocks) == 1
        assert blocks[0][2] == "python"

    def test_detect_code_blocks_multiple(self) -> None:
        """Test detecting multiple code blocks."""
        log_text = """
```python
code1
```
```javascript
code2
```
"""
        blocks = detect_code_blocks(log_text)

        assert len(blocks) == 2

    def test_detect_code_blocks_none(self) -> None:
        """Test no code blocks detected."""
        log_text = "Just plain text\nNo code here"

        blocks = detect_code_blocks(log_text)

        assert len(blocks) == 0

    def test_has_code_blocks_true(self) -> None:
        """Test has_code_blocks returns True."""
        log_text = "Text\n```python\ncode\n```"

        assert has_code_blocks(log_text) is True

    def test_has_code_blocks_false(self) -> None:
        """Test has_code_blocks returns False."""
        log_text = "Plain text only"

        assert has_code_blocks(log_text) is False

    def test_log_viewer_creation(self) -> None:
        """Test log viewer can be created."""
        log_text = "Test log"

        viewer = LogViewer(log_text)

        assert viewer._log_text == log_text

    def test_log_viewer_custom_theme(self) -> None:
        """Test log viewer with custom theme."""
        log_text = "Test log"

        viewer = LogViewer(log_text, theme="dracula", line_numbers=True)

        assert viewer._theme == "dracula"
        assert viewer._line_numbers is True


# ---------------------------------------------------------------------------
# Diff folding tests (T593)
# ---------------------------------------------------------------------------


def _make_diff(n_changed: int = 30, filename: str = "foo.py") -> str:
    """Build a minimal synthetic unified diff with *n_changed* +/- lines."""
    lines = [
        f"diff --git a/{filename} b/{filename}",
        f"--- a/{filename}",
        f"+++ b/{filename}",
        "@@ -1,5 +1,5 @@",
    ]
    for i in range(n_changed // 2):
        lines.append(f"-old line {i}")
        lines.append(f"+new line {i}")
    return "\n".join(lines)


class TestDetectDiffBlocks:
    """Tests for detect_diff_blocks."""

    def test_no_diff_returns_empty(self) -> None:
        assert detect_diff_blocks("plain log output\nno diffs here") == []

    def test_single_diff_block_detected(self) -> None:
        diff = _make_diff(n_changed=4)
        blocks = detect_diff_blocks(diff)

        assert len(blocks) == 1
        assert isinstance(blocks[0], DiffBlock)
        assert blocks[0].start_line == 0

    def test_two_diff_blocks_detected(self) -> None:
        diff = _make_diff(n_changed=4, filename="a.py") + "\n" + _make_diff(n_changed=4, filename="b.py")
        blocks = detect_diff_blocks(diff)

        assert len(blocks) == 2

    def test_diff_block_added_removed_counts(self) -> None:
        diff = _make_diff(n_changed=10)  # 5 removed + 5 added
        blocks = detect_diff_blocks(diff)

        assert blocks[0].added == 5
        assert blocks[0].removed == 5

    def test_diff_block_lines_not_empty(self) -> None:
        diff = _make_diff(n_changed=6)
        blocks = detect_diff_blocks(diff)

        assert len(blocks[0].lines) > 0

    def test_surrounding_text_not_captured(self) -> None:
        text = "Agent output:\n" + _make_diff(n_changed=4) + "\nDone."
        blocks = detect_diff_blocks(text)

        # Block starts at the diff --git line, not line 0
        assert blocks[0].start_line > 0


class TestFoldDiffLines:
    """Tests for fold_diff_lines."""

    def test_short_block_returned_unchanged(self) -> None:
        lines = ["line1", "line2", "line3"]
        result = fold_diff_lines(lines, max_lines=10)
        assert result == lines

    def test_long_block_truncated(self) -> None:
        lines = [f"line {i}" for i in range(50)]
        result = fold_diff_lines(lines, max_lines=10)
        assert len(result) == 11  # 10 kept + 1 summary

    def test_fold_summary_contains_folded_count(self) -> None:
        lines = [f"line {i}" for i in range(30)]
        result = fold_diff_lines(lines, max_lines=10)
        summary = result[-1]
        assert "20" in summary  # 30 - 10 = 20 folded

    def test_fold_summary_contains_add_remove_counts(self) -> None:
        lines = [
            "diff --git a/foo.py b/foo.py",
            "--- a/foo.py",
            "+++ b/foo.py",
            "@@ -1 +1 @@",
        ]
        for i in range(15):
            lines.append(f"-removed {i}")
        for i in range(10):
            lines.append(f"+added {i}")

        result = fold_diff_lines(lines, max_lines=5)
        summary = result[-1]
        assert "+10" in summary
        assert "-15" in summary

    def test_exact_threshold_not_folded(self) -> None:
        lines = [f"line {i}" for i in range(20)]
        result = fold_diff_lines(lines, max_lines=20)
        assert result == lines  # exactly at threshold — no fold


class TestApplyDiffFolding:
    """Tests for apply_diff_folding."""

    def test_no_diff_text_unchanged(self) -> None:
        text = "plain log output\nno diff blocks"
        assert apply_diff_folding(text) == text

    def test_short_diff_not_folded(self) -> None:
        diff = _make_diff(n_changed=4)  # small diff
        result = apply_diff_folding(diff, max_lines=50)
        # Should be unchanged (no fold summary appended)
        assert "folded" not in result

    def test_long_diff_gets_folded(self) -> None:
        diff = _make_diff(n_changed=60)
        result = apply_diff_folding(diff, max_lines=10)
        assert "folded" in result

    def test_preamble_preserved(self) -> None:
        preamble = "Agent started task:\n"
        diff = _make_diff(n_changed=60)
        text = preamble + diff
        result = apply_diff_folding(text, max_lines=10)

        assert result.startswith(preamble)

    def test_two_diffs_both_folded(self) -> None:
        diff1 = _make_diff(n_changed=60, filename="a.py")
        diff2 = _make_diff(n_changed=60, filename="b.py")
        text = diff1 + "\n" + diff2
        result = apply_diff_folding(text, max_lines=10)
        # Two fold summaries should appear
        assert result.count("folded") == 2

    def test_custom_threshold_respected(self) -> None:
        diff = _make_diff(n_changed=30)  # ~34 lines total
        # With threshold=5 it should fold; with threshold=200 it should not.
        assert "folded" in apply_diff_folding(diff, max_lines=5)
        assert "folded" not in apply_diff_folding(diff, max_lines=200)


# ---------------------------------------------------------------------------
# Markdown detection tests (T594)
# ---------------------------------------------------------------------------


class TestLooksLikeMarkdown:
    """Tests for the _looks_like_markdown heuristic."""

    def test_plain_text_not_markdown(self) -> None:
        text = "Agent is processing the file.\nChecking dependencies.\nAll good."
        assert _looks_like_markdown(text) is False

    def test_heading_detected(self) -> None:
        text = "# Summary\nEverything looks good."
        assert _looks_like_markdown(text) is True

    def test_bold_detected(self) -> None:
        text = "This is **important** information.\nPlease review it."
        assert _looks_like_markdown(text) is True

    def test_list_detected(self) -> None:
        text = "Changes made:\n- Fixed bug A\n- Fixed bug B\n- Added feature C"
        assert _looks_like_markdown(text) is True

    def test_blockquote_detected(self) -> None:
        text = "> Note: this is experimental\nProceed with caution."
        assert _looks_like_markdown(text) is True

    def test_empty_text_not_markdown(self) -> None:
        assert _looks_like_markdown("") is False

    def test_only_whitespace_not_markdown(self) -> None:
        assert _looks_like_markdown("   \n\n  ") is False

    def test_numbered_list_detected(self) -> None:
        text = "Steps:\n1. First step\n2. Second step\n3. Third step"
        assert _looks_like_markdown(text) is True


class TestLogViewerMarkdownAndDiffOptions:
    """Tests for LogViewer constructor options."""

    def test_fold_diffs_enabled_by_default(self) -> None:
        viewer = LogViewer("some text")
        assert viewer._fold_diffs is True

    def test_render_markdown_enabled_by_default(self) -> None:
        viewer = LogViewer("some text")
        assert viewer._render_markdown is True

    def test_fold_diffs_can_be_disabled(self) -> None:
        viewer = LogViewer("some text", fold_diffs=False)
        assert viewer._fold_diffs is False

    def test_render_markdown_can_be_disabled(self) -> None:
        viewer = LogViewer("some text", render_markdown=False)
        assert viewer._render_markdown is False

    def test_custom_diff_fold_threshold(self) -> None:
        viewer = LogViewer("some text", diff_fold_threshold=5)
        assert viewer._diff_fold_threshold == 5

    def test_preprocess_folds_diff_when_enabled(self) -> None:
        diff = _make_diff(n_changed=60)
        viewer = LogViewer(diff, fold_diffs=True, diff_fold_threshold=10)
        processed = viewer._preprocess(diff)
        assert "folded" in processed

    def test_preprocess_skips_fold_when_disabled(self) -> None:
        diff = _make_diff(n_changed=60)
        viewer = LogViewer(diff, fold_diffs=False)
        processed = viewer._preprocess(diff)
        assert "folded" not in processed
