"""Context token accounting — analyze where context budget is spent.

Provides ``analyze_context()`` that returns per-category token counts
for a given prompt or context string, and ``format_context_breakdown()``
for displaying the breakdown with percentages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class TokenBreakdown:
    """Per-category token count breakdown.

    Attributes:
        system_prompt: Tokens used by the system prompt / role definition.
        task_description: Tokens used by the task description.
        context_files: Tokens used by included context files.
        rag_chunks: Tokens used by RAG / knowledge-base chunks.
        lessons: Tokens used by lessons / past failures.
        tools: Tokens used by tool definitions / MCP schemas.
        other: Tokens in sections not classified above.
        total: Total token count for the entire prompt.
    """

    system_prompt: int = 0
    task_description: int = 0
    context_files: int = 0
    rag_chunks: int = 0
    lessons: int = 0
    tools: int = 0
    other: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, int]:
        """Return breakdown as a dict."""
        return {
            "system_prompt": self.system_prompt,
            "task_description": self.task_description,
            "context_files": self.context_files,
            "rag_chunks": self.rag_chunks,
            "lessons": self.lessons,
            "tools": self.tools,
            "other": self.other,
            "total": self.total,
        }

    def percentages(self) -> dict[str, float]:
        """Return breakdown as percentages of total."""
        if self.total == 0:
            return {k: 0.0 for k in self.to_dict()}
        return {k: round(v / self.total * 100, 1) for k, v in self.to_dict().items()}

    def category_names(self) -> list[str]:
        """Return list of non-zero category names."""
        result: list[str] = []
        if self.system_prompt > 0:
            result.append("system_prompt")
        if self.task_description > 0:
            result.append("task_description")
        if self.context_files > 0:
            result.append("context_files")
        if self.rag_chunks > 0:
            result.append("rag_chunks")
        if self.lessons > 0:
            result.append("lessons")
        if self.tools > 0:
            result.append("tools")
        if self.other > 0:
            result.append("other")
        return result


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

# Simple word-based token estimate (~4 chars per token for English)
_CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Estimate token count for a string.

    Uses a simple heuristic of 4 chars per token (approximate for English).
    Falls back to more precise estimation for non-ASCII text.

    Args:
        text: The text to count tokens for.

    Returns:
        Estimated token count.
    """
    if not text:
        return 0

    # Try tiktoken first (more accurate)
    try:
        import tiktoken  # pyright: ignore[reportMissingImports]

        enc = tiktoken.get_encoding("cl100k_base")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return len(enc.encode(text))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    except (ImportError, RuntimeError):
        pass

    # Fallback: word-based heuristic
    words = len(text.split())
    chars = len(text)

    # Average of word-count and char-count estimates
    return max(words, chars // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Context analysis
# ---------------------------------------------------------------------------


def _extract_sections(prompt: str) -> dict[str, str]:
    """Extract named sections from a prompt string.

    Looks for markdown headers that identify sections like:
    - ## System prompt / Role
    - ## Task description
    - ## Context / File context
    - ## RAG / Knowledge base
    - ## Lessons
    - ## Tools / Tool definitions

    Args:
        prompt: The full prompt text.

    Returns:
        Dict mapping section name to section text.
    """
    sections: dict[str, str] = {}

    # Common section header patterns
    header_patterns = [
        (r"(?i)^#{1,3}\s*(?:system\s*prompt|role\b|identity\b|instructions\b)", "system_prompt"),
        (r"(?i)^#{1,3}\s*(?:task\b|objective\b|goal\b|assignment\b|description\b)", "task_description"),
        (r"(?i)^#{1,3}\s*(?:context\b|file\s*context\b|project\s*context\b|workspace\b)", "context_files"),
        (r"(?i)^#{1,3}\s*(?:rag\b|knowledge\s*base\b|retrieval\b|index\b)", "rag_chunks"),
        (r"(?i)^#{1,3}\s*(?:lessons?\b|past\s*failures?\b|learnings?\b)", "lessons"),
        (r"(?i)^#{1,3}\s*(?:tools?\b|tool\s*definitions?\b|mcp\b|functions?\b)", "tools"),
    ]

    lines = prompt.splitlines()
    current_section: str | None = None
    current_text: list[str] = []

    for line in lines:
        matched = False
        for pattern, section_name in header_patterns:
            if re.match(pattern, line, re.MULTILINE):
                # Save previous section
                if current_section and current_text:
                    sections[current_section] = "\n".join(current_text)
                current_section = section_name
                current_text = [line]
                matched = True
                break

        if not matched and current_section:
            current_text.append(line)

    # Save last section
    if current_section and current_text:
        sections[current_section] = "\n".join(current_text)

    return sections


def analyze_context(prompt: str) -> TokenBreakdown:
    """Analyze token usage across categories in a prompt.

    Attempts to categorize sections of the prompt by looking for
    markdown headers and known patterns. Falls back to a simple
    split for prompts without clear section markers.

    Args:
        prompt: The full prompt text to analyze.

    Returns:
        TokenBreakdown with per-category token counts.
    """
    sections = _extract_sections(prompt)
    total = count_tokens(prompt)

    breakdown = TokenBreakdown(total=total)

    # Count tokens for each extracted section
    if "system_prompt" in sections:
        breakdown.system_prompt = count_tokens(sections["system_prompt"])
    if "task_description" in sections:
        breakdown.task_description = count_tokens(sections["task_description"])
    if "context_files" in sections:
        breakdown.context_files = count_tokens(sections["context_files"])
    if "rag_chunks" in sections:
        breakdown.rag_chunks = count_tokens(sections["rag_chunks"])
    if "lessons" in sections:
        breakdown.lessons = count_tokens(sections["lessons"])
    if "tools" in sections:
        breakdown.tools = count_tokens(sections["tools"])

    # Calculate "other" as the remainder
    accounted = (
        breakdown.system_prompt
        + breakdown.task_description
        + breakdown.context_files
        + breakdown.rag_chunks
        + breakdown.lessons
        + breakdown.tools
    )
    breakdown.other = max(0, total - accounted)

    return breakdown


# ---------------------------------------------------------------------------
# File-based context analysis
# ---------------------------------------------------------------------------


def analyze_context_file(path: Path) -> TokenBreakdown:
    """Analyze a prompt/context file for token usage.

    Args:
        path: Path to the prompt or context file.

    Returns:
        TokenBreakdown for the file contents.
    """
    if not path.exists():
        return TokenBreakdown()
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return TokenBreakdown()
    return analyze_context(content)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_context_breakdown(breakdown: TokenBreakdown, show_empty: bool = False) -> str:
    """Format a token breakdown as a human-readable string.

    Args:
        breakdown: The token breakdown to format.
        show_empty: If True, include categories with 0 tokens.

    Returns:
        Formatted string suitable for console output.
    """
    if breakdown.total == 0:
        return "Context: (empty)"

    pcts = breakdown.percentages()
    lines: list[str] = []
    lines.append(f"Context budget: {breakdown.total:,} tokens")

    categories = [
        ("system_prompt", "System prompt"),
        ("task_description", "Task description"),
        ("context_files", "Context files"),
        ("rag_chunks", "RAG / KB"),
        ("lessons", "Lessons"),
        ("tools", "Tool defs"),
        ("other", "Other"),
    ]

    for key, label in categories:
        tokens = getattr(breakdown, key)
        pct = pcts[key]
        if tokens > 0 or show_empty:
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"  {label:18s} {tokens:>6,} ({pct:5.1f}%) {bar}")

    return "\n".join(lines)
