"""Syntax highlighting, diff folding, and markdown rendering for agent logs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult

CODE_BLOCK_PATTERN = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)

# ---------------------------------------------------------------------------
# Diff folding (T593)
# ---------------------------------------------------------------------------

# A diff block starts with a "diff --git" line or a unified diff header.
_DIFF_START_PATTERN = re.compile(r"^diff --git ", re.MULTILINE)

# Default maximum number of diff lines to show before folding the rest.
_DIFF_FOLD_THRESHOLD = 20


@dataclass
class DiffBlock:
    """A detected diff block within a log text.

    Attributes:
        start_line: Index (0-based) of the first line of the diff.
        end_line: Index (exclusive) of the last line of the diff.
        lines: Raw lines of the diff block.
    """

    start_line: int
    end_line: int
    lines: list[str] = field(default_factory=list[str])

    @property
    def added(self) -> int:
        """Count of added lines (starting with '+' but not '+++')."""
        return sum(1 for ln in self.lines if ln.startswith("+") and not ln.startswith("+++"))

    @property
    def removed(self) -> int:
        """Count of removed lines (starting with '-' but not '---')."""
        return sum(1 for ln in self.lines if ln.startswith("-") and not ln.startswith("---"))


def detect_diff_blocks(text: str) -> list[DiffBlock]:
    """Detect unified diff blocks in *text*.

    A new block starts at every ``diff --git`` line.  Everything up to (but
    not including) the next ``diff --git`` line — or the end of the text —
    belongs to that block.

    Args:
        text: Multi-line log text to scan.

    Returns:
        List of :class:`DiffBlock` instances in document order.
    """
    all_lines = text.splitlines()
    blocks: list[DiffBlock] = []

    start: int | None = None
    for i, line in enumerate(all_lines):
        if line.startswith("diff --git "):
            if start is not None:
                blocks.append(DiffBlock(start_line=start, end_line=i, lines=all_lines[start:i]))
            start = i
    if start is not None:
        blocks.append(DiffBlock(start_line=start, end_line=len(all_lines), lines=all_lines[start:]))
    return blocks


def fold_diff_lines(lines: list[str], max_lines: int = _DIFF_FOLD_THRESHOLD) -> list[str]:
    """Truncate *lines* to *max_lines* and append a fold summary.

    If the block is already short enough it is returned unchanged.

    Args:
        lines: Raw diff lines for a single block.
        max_lines: Maximum number of lines to keep before folding.

    Returns:
        Possibly-truncated list of lines.
    """
    if len(lines) <= max_lines:
        return lines

    kept = lines[:max_lines]
    folded_count = len(lines) - max_lines
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    kept.append(f"  [dim]... {folded_count} lines folded  [green]+{added}[/green] [red]-{removed}[/red][/dim]")
    return kept


def apply_diff_folding(text: str, max_lines: int = _DIFF_FOLD_THRESHOLD) -> str:
    """Return *text* with long diff blocks folded.

    Diff blocks exceeding *max_lines* are truncated; a summary line
    (``… N lines folded  +A -R``) is appended to each truncated block.

    Args:
        text: Raw log text that may contain unified diff output.
        max_lines: Maximum diff lines to show per block before folding.

    Returns:
        Modified text with large diffs replaced by folded representations.
    """
    blocks = detect_diff_blocks(text)
    if not blocks:
        return text

    all_lines = text.splitlines(keepends=True)
    result: list[str] = []
    prev_end = 0

    for block in blocks:
        # Copy lines before this block verbatim.
        result.extend(all_lines[prev_end : block.start_line])

        raw_block_lines = [ln.rstrip("\n") for ln in all_lines[block.start_line : block.end_line]]
        folded = fold_diff_lines(raw_block_lines, max_lines=max_lines)
        result.extend(ln + "\n" for ln in folded)

        prev_end = block.end_line

    result.extend(all_lines[prev_end:])
    return "".join(result)


# ---------------------------------------------------------------------------
# Markdown detection (T594)
# ---------------------------------------------------------------------------

# Patterns that suggest a line is markdown-formatted.
_MD_HEADING = re.compile(r"^#{1,6} ")
_MD_BOLD = re.compile(r"\*\*.+?\*\*")
_MD_ITALIC = re.compile(r"\*.+?\*")
_MD_LIST = re.compile(r"^(\s*[-*+]|\s*\d+\.) ")
_MD_BLOCKQUOTE = re.compile(r"^> ")
_MD_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")

_MD_THRESHOLD = 0.15  # fraction of lines that must look like markdown


def _looks_like_markdown(text: str) -> bool:
    """Heuristic: return True when *text* has enough markdown indicators.

    Args:
        text: Text block to evaluate.

    Returns:
        True when the block appears to be markdown-formatted prose.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    md_lines = sum(
        1
        for ln in lines
        if (
            _MD_HEADING.match(ln)
            or _MD_BOLD.search(ln)
            or _MD_LIST.match(ln)
            or _MD_BLOCKQUOTE.match(ln)
            or _MD_HR.match(ln.strip())
        )
    )
    return md_lines / len(lines) >= _MD_THRESHOLD


# ---------------------------------------------------------------------------
# Combined log renderer
# ---------------------------------------------------------------------------


class LogViewer:
    """Renders agent log with syntax highlighting for code blocks.

    Detects fenced code blocks (```) and renders them with Rich Syntax
    highlighting. Non-code lines rendered as plain text.
    """

    def __init__(
        self,
        log_text: str,
        theme: str = "monokai",
        line_numbers: bool = False,
        fold_diffs: bool = True,
        render_markdown: bool = True,
        diff_fold_threshold: int = _DIFF_FOLD_THRESHOLD,
    ) -> None:
        """Initialize log viewer.

        Args:
            log_text: Raw log text to render.
            theme: Pygments syntax theme.
            line_numbers: Whether to show line numbers in code blocks.
            fold_diffs: When True, long diff blocks are folded.
            render_markdown: When True, markdown-looking prose is rendered
                with Rich Markdown.
            diff_fold_threshold: Maximum diff lines before folding.
        """
        self._log_text = log_text
        self._theme = theme
        self._line_numbers = line_numbers
        self._fold_diffs = fold_diffs
        self._render_markdown = render_markdown
        self._diff_fold_threshold = diff_fold_threshold

    def _preprocess(self, text: str) -> str:
        """Apply diff folding if enabled.

        Args:
            text: Raw log text.

        Returns:
            Preprocessed text.
        """
        if self._fold_diffs:
            text = apply_diff_folding(text, max_lines=self._diff_fold_threshold)
        return text

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render log with syntax highlighting and optional markdown.

        Args:
            console: Rich console.
            options: Console options.

        Yields:
            Rich segments.
        """
        processed = self._preprocess(self._log_text)
        parts = CODE_BLOCK_PATTERN.split(processed)

        for i, part in enumerate(parts):
            if not part:
                continue

            if i % 3 == 1:
                # language tag — skip, consumed by i%3==2
                continue
            elif i % 3 == 2:
                language = parts[i - 1] or "text"
                syntax = Syntax(
                    part,
                    language,
                    theme=self._theme,
                    line_numbers=self._line_numbers,
                )
                yield from console.render(syntax, options)
            else:
                # Plain text section — try markdown rendering.
                if self._render_markdown and _looks_like_markdown(part):
                    yield from console.render(Markdown(part), options)
                else:
                    yield Text(part)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def detect_code_blocks(log_text: str) -> list[tuple[int, int, str]]:
    """Detect code blocks in log text.

    Args:
        log_text: Log text to search.

    Returns:
        List of (start, end, language) tuples for each code block.
    """
    blocks: list[tuple[int, int, str]] = []
    for match in CODE_BLOCK_PATTERN.finditer(log_text):
        language = match.group(1) or "text"
        blocks.append((match.start(), match.end(), language))
    return blocks


def has_code_blocks(log_text: str) -> bool:
    """Check if log text contains code blocks.

    Args:
        log_text: Log text to check.

    Returns:
        True if code blocks detected.
    """
    return bool(CODE_BLOCK_PATTERN.search(log_text))
