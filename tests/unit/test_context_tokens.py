"""Tests for context_tokens — context token accounting."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.context_tokens import (
    TokenBreakdown,
    _extract_sections,
    analyze_context,
    analyze_context_file,
    count_tokens,
    format_context_breakdown,
)


@pytest.fixture()
def sample_prompt() -> str:
    """Sample prompt with clear sections."""
    return """# System prompt
You are a helpful assistant that writes code.

## Task
Implement feature X for the project.

## Context files
Here is the current file tree:
- src/main.py
- src/utils.py
- tests/test_main.py

## RAG
Relevant code snippets from the codebase...

## Lessons
Previously failed when trying to use numpy without importing.

## Tools
You have access to:
- read_file
- write_file
- shell
"""


@pytest.fixture()
def mixed_prompt() -> str:
    """Prompt without clear section headers."""
    return "Hello world, this is a simple prompt without sections."


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    """Create a sample prompt file."""
    f = tmp_path / "prompt.md"
    f.write_text(
        "# System prompt\nYou are a backend developer.\n## Task\nWrite tests.\n",
        encoding="utf-8",
    )
    return f


# --- TestTokenBreakdown ---


class TestTokenBreakdown:
    def test_to_dict(self) -> None:
        bd = TokenBreakdown(system_prompt=100, total=500)
        d = bd.to_dict()
        assert d["system_prompt"] == 100
        assert d["total"] == 500
        assert d["task_description"] == 0

    def test_percentages_zero_total(self) -> None:
        bd = TokenBreakdown()
        pcts = bd.percentages()
        assert pcts["total"] == 0.0

    def test_percentages(self) -> None:
        bd = TokenBreakdown(system_prompt=100, task_description=200, other=200, total=500)
        pcts = bd.percentages()
        assert pcts["system_prompt"] == 20.0
        assert pcts["task_description"] == 40.0
        assert pcts["other"] == 40.0

    def test_category_names(self) -> None:
        bd = TokenBreakdown(system_prompt=100, context_files=50, other=0, total=150)
        names = bd.category_names()
        assert "system_prompt" in names
        assert "context_files" in names
        assert "other" not in names


# --- TestCountTokens ---


class TestCountTokens:
    def test_empty_string(self) -> None:
        assert count_tokens("") == 0

    def test_short_text(self) -> None:
        assert count_tokens("Hello world") > 0

    def test_long_text(self) -> None:
        text = "The quick brown fox jumps over the lazy dog. " * 100
        tokens = count_tokens(text)
        # ~900 tokens expected (45 words * 100 = 4500 words)
        assert tokens > 500


# --- TestExtractSections ---


class TestExtractSections:
    def test_extracts_system_prompt(self, sample_prompt: str) -> None:
        sections = _extract_sections(sample_prompt)
        assert "system_prompt" in sections
        assert "helpful assistant" in sections["system_prompt"]

    def test_extracts_task(self, sample_prompt: str) -> None:
        sections = _extract_sections(sample_prompt)
        assert "task_description" in sections
        assert "Implement feature X" in sections["task_description"]

    def test_extracts_context_files(self, sample_prompt: str) -> None:
        sections = _extract_sections(sample_prompt)
        assert "context_files" in sections
        assert "src/main.py" in sections["context_files"]

    def test_extracts_rag(self, sample_prompt: str) -> None:
        sections = _extract_sections(sample_prompt)
        assert "rag_chunks" in sections
        assert "Relevant code snippets" in sections["rag_chunks"]

    def test_extracts_lessons(self, sample_prompt: str) -> None:
        sections = _extract_sections(sample_prompt)
        assert "lessons" in sections
        assert "Previously failed" in sections["lessons"]

    def test_extracts_tools(self, sample_prompt: str) -> None:
        sections = _extract_sections(sample_prompt)
        assert "tools" in sections
        assert "read_file" in sections["tools"]

    def test_no_sections(self, mixed_prompt: str) -> None:
        sections = _extract_sections(mixed_prompt)
        assert len(sections) == 0


# --- TestAnalyzeContext ---


class TestAnalyzeContext:
    def test_sample_prompt(self, sample_prompt: str) -> None:
        bd = analyze_context(sample_prompt)
        assert bd.total > 0
        assert bd.system_prompt > 0
        assert bd.task_description > 0
        assert bd.context_files > 0
        assert bd.rag_chunks > 0
        assert bd.lessons > 0
        assert bd.tools > 0

    def test_empty_prompt(self) -> None:
        bd = analyze_context("")
        assert bd.total == 0
        assert bd.other == 0

    def test_plain_prompt(self, mixed_prompt: str) -> None:
        bd = analyze_context(mixed_prompt)
        assert bd.total > 0
        # Plain prompt → all tokens go to "other"
        assert bd.other == bd.total

    def test_sections_sum_roughly_equals_total(self, sample_prompt: str) -> None:
        bd = analyze_context(sample_prompt)
        accounted = (
            bd.system_prompt + bd.task_description + bd.context_files + bd.rag_chunks + bd.lessons + bd.tools + bd.other
        )
        # Allow ±10% difference due to header overlap
        assert abs(accounted - bd.total) <= max(bd.total * 0.1, 5)


# --- TestAnalyzeContextFile ---


class TestAnalyzeContextFile:
    def test_reads_file(self, sample_file: Path) -> None:
        bd = analyze_context_file(sample_file)
        assert bd.total > 0
        assert bd.system_prompt > 0

    def test_missing_file(self, tmp_path: Path) -> None:
        bd = analyze_context_file(tmp_path / "missing.md")
        assert bd.total == 0


# --- TestFormatContextBreakdown ---


class TestFormatContextBreakdown:
    def test_empty(self) -> None:
        bd = TokenBreakdown()
        output = format_context_breakdown(bd)
        assert "empty" in output.lower()

    def test_with_content(self, sample_prompt: str) -> None:
        bd = analyze_context(sample_prompt)
        output = format_context_breakdown(bd)
        assert "Context budget" in output
        assert "%" in output

    def test_show_empty(self) -> None:
        bd = TokenBreakdown(system_prompt=100, total=100)
        output = format_context_breakdown(bd, show_empty=True)
        assert "Task description" in output
        assert "0" in output  # shows 0 tokens

    def test_hides_empty_by_default(self) -> None:
        bd = TokenBreakdown(system_prompt=100, total=100)
        output = format_context_breakdown(bd)
        # Other categories should not appear if they have 0 tokens
        assert "RAG" not in output
        assert "Lessons" not in output
