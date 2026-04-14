"""Word-level diff rendering for Bernstein TUI.

Provides utilities to compute word-level diffs between two lines and render
them with Rich-compatible markup (green strikethrough for removed words,
green bold for added words).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


# Word boundary: sequence of word characters or whitespace blocks
_WORD_RE = re.compile(r"\w+|\s+|[^\w\s]+")


def _apply_opcode(
    tag: str,
    old_tokens: list[str],
    new_tokens: list[str],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    old_result: list[tuple[str, bool]],
    new_result: list[tuple[str, bool]],
) -> None:
    """Apply a single SequenceMatcher opcode to result lists."""
    if tag == "equal":
        old_result.extend((tok, False) for tok in old_tokens[i1:i2])
        new_result.extend((tok, False) for tok in new_tokens[j1:j2])
    elif tag == "replace":
        old_result.extend((tok, True) for tok in old_tokens[i1:i2])
        new_result.extend((tok, True) for tok in new_tokens[j1:j2])
    elif tag == "delete":
        old_result.extend((tok, True) for tok in old_tokens[i1:i2])
    elif tag == "insert":
        new_result.extend((tok, True) for tok in new_tokens[j1:j2])


def _tokenize(line: str) -> list[str]:
    """Split a line into word-level tokens.

    Preserves whitespace runs and punctuation as separate tokens.

    Args:
        line: The line to tokenize.

    Returns:
        List of token strings.
    """
    return _WORD_RE.findall(line)


def word_diff(
    old_line: str,
    new_line: str,
) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """Compute word-level diff between two lines.

    Args:
        old_line: The original line text.
        new_line: The modified line text.

    Returns:
        A tuple of ``(old_tokens, new_tokens)`` where each element is a list
        of ``(token, is_changed)`` pairs.  ``is_changed`` is ``True`` for
        tokens that were added or removed, ``False`` for tokens that are
        unchanged in both lines.
    """
    old_tokens = _tokenize(old_line)
    new_tokens = _tokenize(new_line)

    matcher = SequenceMatcher(None, old_tokens, new_tokens)
    opcodes = matcher.get_opcodes()

    old_result: list[tuple[str, bool]] = []
    new_result: list[tuple[str, bool]] = []

    for tag, i1, i2, j1, j2 in opcodes:
        _apply_opcode(tag, old_tokens, new_tokens, i1, i2, j1, j2, old_result, new_result)

    return old_result, new_result


def _render_tokens(tokens: list[tuple[str, bool]], removed: bool) -> str:
    """Render a list of ``(token, is_changed)`` as Rich markup.

    Args:
        tokens: List of (token, is_changed) tuples.
        removed: If ``True``, mark changed tokens as removed (strikethrough);
            otherwise mark them as added (bold green).

    Returns:
        A string containing Rich-compatible markup for the token list.
    """
    parts: list[str] = []
    for token, is_changed in tokens:
        escaped = token.replace("[", r"\[").replace("]", r"\]")
        if is_changed:
            if removed:
                parts.append(f"[strike green]{escaped}[/]")
            else:
                parts.append(f"[bold green]{escaped}[/]")
        else:
            parts.append(escaped)
    return "".join(parts)


def render_word_diff(console: Console, old_line: str, new_line: str) -> None:
    """Render a word-level diff between two lines to the console.

    Removed words appear as green strikethrough; added words as green bold.
    Unchanged text renders as plain white.

    Args:
        console: A Rich ``Console`` instance to print to.
        old_line: The original line text.
        new_line: The modified line text.
    """
    old_tokens, new_tokens = word_diff(old_line, new_line)

    old_markup = _render_tokens(old_tokens, removed=True)
    new_markup = _render_tokens(new_tokens, removed=False)

    console.print(f"  [dim]-[/] {old_markup}")
    console.print(f"  [dim]+[/] {new_markup}")
