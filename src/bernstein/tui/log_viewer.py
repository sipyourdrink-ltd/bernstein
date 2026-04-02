"""Syntax highlighting for code in agent logs."""

from __future__ import annotations

import re
from typing import Iterable

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text


# Pattern to match fenced code blocks in logs
CODE_BLOCK_PATTERN = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)


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
    ) -> None:
        """Initialize log viewer.

        Args:
            log_text: Raw log text to render.
            theme: Pygments syntax theme.
            line_numbers: Whether to show line numbers in code blocks.
        """
        self._log_text = log_text
        self._theme = theme
        self._line_numbers = line_numbers

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render log with syntax highlighting.

        Args:
            console: Rich console.
            options: Console options.

        Yields:
            Rich segments.
        """
        # Split by code blocks
        parts = CODE_BLOCK_PATTERN.split(self._log_text)

        for i, part in enumerate(parts):
            if not part:
                continue

            # Every 3rd part (starting from index 1) is a code block language
            if i % 3 == 1:
                # Language specifier (may be empty)
                continue
            elif i % 3 == 2:
                # Code block content
                language = parts[i - 1] or "text"
                syntax = Syntax(
                    part,
                    language,
                    theme=self._theme,
                    line_numbers=self._line_numbers,
                )
                yield from console.render(syntax, options)
            else:
                # Plain text
                yield Text(part)


def detect_code_blocks(log_text: str) -> list[tuple[int, int, str]]:
    """Detect code blocks in log text.

    Args:
        log_text: Log text to search.

    Returns:
        List of (start, end, language) tuples for each code block.
    """
    blocks = []
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
