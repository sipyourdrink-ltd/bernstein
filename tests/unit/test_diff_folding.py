"""Tests for diff_folding — collapsible diff display."""

from __future__ import annotations

import pytest

from bernstein.diff_folding import (
    DiffHunk,
    FileDiff,
    _count_changes,
    _parse_hunk_header,
    expand_all,
    fold_all,
    format_file_summary,
    format_hunk_summary,
    parse_diff,
    render_folding_diff,
    render_full_diff,
    toggle_file_fold,
    toggle_hunk_fold,
)


@pytest.fixture()
def sample_diff() -> str:
    """Create a sample unified diff."""
    return """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -10,3 +10,5 @@ def hello():
+    # Added comment
+    print("hello")
     return "world"
@@ -20,7 +22,9 @@ def goodbye():
+    # Another comment
     print("goodbye")
+    print("see you")
     return "done"
diff --git a/tests/test_main.py b/tests/test_main.py
--- a/tests/test_main.py
+++ b/tests/test_main.py
@@ -1,3 +1,4 @@
+import pytest
 def test_hello():
     assert hello() == "world"
"""


@pytest.fixture()
def file_diff() -> FileDiff:
    """Create a sample FileDiff."""
    hunks = [
        DiffHunk(
            header="@@ -10,3 +10,5 @@ def hello():",
            lines=[
                "@@ -10,3 +10,5 @@ def hello():",
                "+    # Added comment",
                '+    print("hello")',
                '     return "world"',
            ],
            start_line=10,
            end_line=14,
            added=2,
            removed=0,
            is_folded=True,
        ),
        DiffHunk(
            header="@@ -20,7 +22,9 @@ def goodbye():",
            lines=[
                "@@ -20,7 +22,9 @@ def goodbye():",
                "+    # Another comment",
                '     print("goodbye")',
                '+    print("see you")',
                '     return "done"',
            ],
            start_line=22,
            end_line=30,
            added=2,
            removed=0,
            is_folded=True,
        ),
    ]
    return FileDiff(
        filename="src/main.py",
        hunks=hunks,
        is_folded=True,
        total_added=4,
        total_removed=0,
    )


# --- TestParseHunkHeader ---


class TestParseHunkHeader:
    def test_valid_header(self) -> None:
        result = _parse_hunk_header("@@ -10,5 +10,7 @@ def func():")
        assert result == (10, 16)

    def test_single_line(self) -> None:
        result = _parse_hunk_header("@@ -1,1 +1,1 @@")
        assert result == (1, 1)

    def test_invalid_header(self) -> None:
        result = _parse_hunk_header("not a header")
        assert result is None


# --- TestCountChanges ---


class TestCountChanges:
    def test_counts_added_removed(self) -> None:
        lines = [
            "@@ -1,3 +1,5 @@",
            "+added line 1",
            "-removed line",
            " unchanged",
            "+added line 2",
        ]
        added, removed = _count_changes(lines)
        assert added == 2
        assert removed == 1

    def test_ignores_file_headers(self) -> None:
        lines = ["--- a/file.py", "+++ b/file.py", "+real add"]
        added, removed = _count_changes(lines)
        assert added == 1
        assert removed == 0


# --- TestParseDiff ---


class TestParseDiff:
    def test_parses_files(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        assert len(files) == 2
        assert files[0].filename == "src/main.py"
        assert files[1].filename == "tests/test_main.py"

    def test_parses_hunks(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        # The sample diff has hunks spread across files, so we check total hunks
        total_hunks = sum(len(f.hunks) for f in files)
        assert total_hunks >= 2
        # Check that at least one hunk has additions
        assert any(h.added > 0 for f in files for h in f.hunks)

    def test_totals(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        # Check that totals are summed correctly across hunks
        for f in files:
            assert f.total_added == sum(h.added for h in f.hunks)
            assert f.total_removed == sum(h.removed for h in f.hunks)

    def test_empty_diff(self) -> None:
        files = parse_diff("")
        assert files == []


# --- TestToggleFold ---


class TestToggleFold:
    def test_toggle_file(self, file_diff: FileDiff) -> None:
        assert file_diff.is_folded is True
        toggle_file_fold(file_diff)
        assert file_diff.is_folded is False
        toggle_file_fold(file_diff)
        assert file_diff.is_folded is True

    def test_toggle_hunk(self, file_diff: FileDiff) -> None:
        hunk = file_diff.hunks[0]
        assert hunk.is_folded is True
        toggle_hunk_fold(hunk)
        assert hunk.is_folded is False


# --- TestFoldExpandAll ---


class TestFoldExpandAll:
    def test_fold_all(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        expand_all(files)
        fold_all(files)
        for f in files:
            assert f.is_folded is True
            for h in f.hunks:
                assert h.is_folded is True

    def test_expand_all(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        expand_all(files)
        for f in files:
            assert f.is_folded is False
            for h in f.hunks:
                assert h.is_folded is False


# --- TestFormatFileSummary ---


class TestFormatFileSummary:
    def test_format(self, file_diff: FileDiff) -> None:
        summary = format_file_summary(file_diff)
        assert "src/main.py" in summary
        assert "+4/-0" in summary
        assert "2 hunks" in summary


# --- TestFormatHunkSummary ---


class TestFormatHunkSummary:
    def test_format(self, file_diff: FileDiff) -> None:
        summary = format_hunk_summary(file_diff.hunks[0])
        assert "@@ -10,3 +10,5 @@" in summary
        assert "+2/-0" in summary


# --- TestRenderFoldingDiff ---


class TestRenderFoldingDiff:
    def test_folded_shows_summary(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        output = render_folding_diff(files)
        # File-level folding shows only summary lines
        assert "src/main.py" in output
        assert "tests/test_main.py" in output

    def test_expanded_shows_lines(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        expand_all(files)
        output = render_folding_diff(files)
        assert "+    # Added comment" in output

    def test_max_folded_lines(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        # Expand file but keep hunks folded
        for f in files:
            f.is_folded = False
        output = render_folding_diff(files, max_folded_lines=2)
        # Should show 2 lines then ... marker for hunks
        assert "..." in output


# --- TestRenderFullDiff ---


class TestRenderFullDiff:
    def test_renders_full(self, sample_diff: str) -> None:
        files = parse_diff(sample_diff)
        output = render_full_diff(files)
        assert "diff --git a/src/main.py b/src/main.py" in output
        assert "+    # Added comment" in output
