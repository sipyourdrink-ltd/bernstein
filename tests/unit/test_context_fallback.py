# pyright: reportPrivateUsage=false
"""Tests for context_fallback module: prompt compaction and 413 detection.

Covers:
- compact_prompt reduces overall size
- Large code block truncation (>100 lines replaced with summary)
- Duplicate section removal
- File listing truncation
- Traceback stripping (keep last frame only)
- should_compact detects 413 / context-overflow patterns in agent logs
- Integration with rate_limit_tracker.detect_failure_type
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.context_fallback import (
    _CODE_BLOCK_MAX_LINES,
    _FILE_LISTING_MAX_ENTRIES,
    CompactionResult,
    _remove_duplicate_sections,
    _strip_verbose_tracebacks,
    _truncate_file_listings,
    _truncate_large_code_blocks,
    compact_prompt,
    should_compact,
)
from bernstein.core.rate_limit_tracker import RateLimitTracker

# ---------------------------------------------------------------------------
# compact_prompt: overall size reduction
# ---------------------------------------------------------------------------


class TestCompactPromptReducesSize:
    """compact_prompt should produce a smaller prompt when compactable content exists."""

    def test_reduces_prompt_with_large_code_block(self) -> None:
        code_lines = "\n".join(f"    line {i}" for i in range(200))
        prompt = f"Task: implement feature\n\n```python\n{code_lines}\n```\n\nDo it."
        _compacted, result = compact_prompt(prompt)
        assert result.compacted_tokens < result.original_tokens
        assert "truncate_code_blocks" in result.strategy_used

    def test_reduces_prompt_with_duplicates(self) -> None:
        section = "This is a duplicated context section with important info.\nIt spans multiple lines."
        prompt = f"{section}\n\n{section}\n\n{section}\n\nTask: do something."
        _compacted, result = compact_prompt(prompt)
        assert result.compacted_tokens < result.original_tokens
        assert "remove_duplicates" in result.strategy_used

    def test_no_change_on_already_compact_prompt(self) -> None:
        prompt = "Implement the login feature. Keep it simple."
        compacted, result = compact_prompt(prompt)
        assert compacted == prompt
        assert result.strategy_used == "none"
        assert result.original_tokens == result.compacted_tokens

    def test_result_is_compaction_result_dataclass(self) -> None:
        _, result = compact_prompt("short prompt")
        assert isinstance(result, CompactionResult)
        assert result.original_tokens > 0
        assert result.compacted_tokens > 0


# ---------------------------------------------------------------------------
# Code block truncation
# ---------------------------------------------------------------------------


class TestCodeBlockTruncation:
    """Fenced code blocks exceeding 100 lines should be truncated."""

    def test_truncates_block_over_100_lines(self) -> None:
        lines = "\n".join(f"line {i}" for i in range(150))
        text = f"Before\n\n```python\n{lines}\n```\n\nAfter"
        result, changed = _truncate_large_code_blocks(text)
        assert changed is True
        assert "truncated" in result
        assert "150 total" in result

    def test_preserves_block_under_100_lines(self) -> None:
        lines = "\n".join(f"line {i}" for i in range(50))
        text = f"```python\n{lines}\n```"
        result, changed = _truncate_large_code_blocks(text)
        assert changed is False
        assert result == text

    def test_preserves_exactly_100_lines(self) -> None:
        lines = "\n".join(f"line {i}" for i in range(_CODE_BLOCK_MAX_LINES))
        text = f"```\n{lines}\n```"
        _result, changed = _truncate_large_code_blocks(text)
        assert changed is False

    def test_keeps_first_10_lines_of_truncated_block(self) -> None:
        lines = "\n".join(f"line {i}" for i in range(200))
        text = f"```\n{lines}\n```"
        result, _ = _truncate_large_code_blocks(text)
        # First 10 lines should be preserved
        for i in range(10):
            assert f"line {i}" in result
        # Line 100 should NOT appear in the result
        assert "line 100\n" not in result

    def test_multiple_blocks_independently_handled(self) -> None:
        small_block = "```\n" + "\n".join(f"s{i}" for i in range(5)) + "\n```"
        large_block = "```\n" + "\n".join(f"l{i}" for i in range(150)) + "\n```"
        text = f"{small_block}\n\nSome text\n\n{large_block}"
        result, changed = _truncate_large_code_blocks(text)
        assert changed is True
        # Small block should be intact
        assert "s0" in result
        assert "s4" in result


# ---------------------------------------------------------------------------
# Duplicate section removal
# ---------------------------------------------------------------------------


class TestDuplicateSectionRemoval:
    """Identical paragraph-level sections should be collapsed."""

    def test_removes_exact_duplicates(self) -> None:
        text = "Section A content.\n\nSection A content.\n\nSection B content."
        result, changed = _remove_duplicate_sections(text)
        assert changed is True
        assert result.count("Section A content.") == 1
        assert "1 duplicate section(s) removed" in result

    def test_no_change_without_duplicates(self) -> None:
        text = "Section A.\n\nSection B.\n\nSection C."
        _result, changed = _remove_duplicate_sections(text)
        assert changed is False

    def test_removes_multiple_duplicates(self) -> None:
        text = "AAA\n\nBBB\n\nAAA\n\nCCC\n\nBBB\n\nAAA"
        result, changed = _remove_duplicate_sections(text)
        assert changed is True
        assert "3 duplicate section(s) removed" in result


# ---------------------------------------------------------------------------
# File listing truncation
# ---------------------------------------------------------------------------


class TestFileListingTruncation:
    """File listings exceeding 50 entries should be truncated."""

    def test_truncates_long_file_listing(self) -> None:
        listing = "\n".join(f"./src/module_{i}.py" for i in range(80))
        text = f"Files:\n{listing}\n\nDone."
        result, changed = _truncate_file_listings(text)
        assert changed is True
        assert "truncated" in result
        assert "80 total" in result

    def test_preserves_short_listing(self) -> None:
        listing = "\n".join(f"./src/module_{i}.py" for i in range(10))
        text = f"Files:\n{listing}\n\nDone."
        _result, changed = _truncate_file_listings(text)
        assert changed is False

    def test_keeps_first_50_entries(self) -> None:
        listing = "\n".join(f"./src/module_{i}.py" for i in range(80))
        text = f"Files:\n{listing}"
        result, changed = _truncate_file_listings(text)
        assert changed is True
        # First 50 entries present
        for i in range(_FILE_LISTING_MAX_ENTRIES):
            assert f"module_{i}.py" in result
        # Entry 79 should NOT be present
        assert "module_79.py" not in result


# ---------------------------------------------------------------------------
# Traceback stripping
# ---------------------------------------------------------------------------


class TestTracebackStripping:
    """Verbose tracebacks should be reduced to the last frame + exception."""

    def test_strips_multi_frame_traceback(self) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 10, in main\n'
            "    result = process()\n"
            '  File "/app/process.py", line 20, in process\n'
            "    data = load()\n"
            '  File "/app/loader.py", line 30, in load\n'
            "    raise ValueError('bad data')\n"
            "ValueError: bad data"
        )
        result, changed = _strip_verbose_tracebacks(tb)
        assert changed is True
        assert "frames omitted" in result
        # Last frame should be preserved
        assert "loader.py" in result
        assert "ValueError: bad data" in result

    def test_preserves_short_traceback(self) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 10, in main\n'
            "    raise RuntimeError('oops')\n"
            "RuntimeError: oops"
        )
        _result, changed = _strip_verbose_tracebacks(tb)
        assert changed is False

    def test_no_change_without_traceback(self) -> None:
        text = "Everything is fine.\nNo errors here."
        _result, changed = _strip_verbose_tracebacks(text)
        assert changed is False


# ---------------------------------------------------------------------------
# should_compact: 413 pattern detection
# ---------------------------------------------------------------------------


class TestShouldCompact:
    """should_compact scans agent logs for 413 / context-overflow patterns."""

    def test_detects_413_in_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("INFO: starting\nERROR: HTTP 413 Request Entity Too Large\n")
        assert should_compact(log) is True

    def test_detects_prompt_too_long(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ERROR: prompt is too long for model context window\n")
        assert should_compact(log) is True

    def test_detects_context_length_exceeded(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text('{"error": {"type": "context_length_exceeded"}}\n')
        assert should_compact(log) is True

    def test_detects_maximum_context_length(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error: exceeds the maximum context length of 200000 tokens\n")
        assert should_compact(log) is True

    def test_detects_token_limit_exceeded(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ERROR: token limit exceeded\n")
        assert should_compact(log) is True

    def test_detects_request_too_large(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ERROR: request too large\n")
        assert should_compact(log) is True

    def test_returns_false_for_clean_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("INFO: Task completed successfully.\nDone.\n")
        assert should_compact(log) is False

    def test_returns_false_for_missing_file(self, tmp_path: Path) -> None:
        assert should_compact(tmp_path / "nonexistent.log") is False

    def test_returns_false_for_empty_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("")
        assert should_compact(log) is False


# ---------------------------------------------------------------------------
# Integration: failure type detection + should_compact agreement
# ---------------------------------------------------------------------------


class TestIntegrationWithFailureDetection:
    """Verify should_compact and detect_failure_type agree on context overflow."""

    @pytest.mark.parametrize(
        "log_content",
        [
            "ERROR: 413 Payload Too Large\n",
            "prompt is too long\n",
            "context_length_exceeded\n",
            "maximum context length exceeded\n",
            "token limit exceeded\n",
            "request too large\n",
            "PromptTooLongError: input exceeds limit\n",
        ],
    )
    def test_should_compact_agrees_with_detect_failure_type(self, tmp_path: Path, log_content: str) -> None:
        log = tmp_path / "agent.log"
        log.write_text(log_content)
        tracker = RateLimitTracker()
        failure_type = tracker.detect_failure_type(log)
        compact_needed = should_compact(log)
        # Both should agree: if detect_failure_type says context_overflow,
        # should_compact should also return True
        if failure_type == "context_overflow":
            assert compact_needed is True
        # And vice versa
        if compact_needed:
            assert failure_type == "context_overflow"

    def test_rate_limit_not_confused_with_context_overflow(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ERROR: 429 Too Many Requests\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) == "rate_limit"
        # should_compact should NOT trigger on a pure 429
        assert should_compact(log) is False
