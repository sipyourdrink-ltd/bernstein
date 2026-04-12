"""Reactive context-overflow fallback: prompt compaction for 413 errors.

When a CLI agent hits a 413 "Request Entity Too Large" or context overflow error,
this module provides standalone prompt compaction utilities that can be used
independently of the full ``CompactionPipeline``.

Strategies applied by ``compact_prompt``:
1. Truncate large code blocks (>100 lines) to a summary comment.
2. Remove duplicate context sections (identical paragraphs).
3. Truncate file listings to the first 50 entries.
4. Strip verbose error tracebacks, keeping only the last frame.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.defaults import TOKEN
from bernstein.core.rate_limit_tracker import RateLimitTracker

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    """Result of a ``compact_prompt`` invocation.

    Attributes:
        original_tokens: Estimated token count of the original prompt.
        compacted_tokens: Estimated token count after compaction.
        strategy_used: Comma-separated list of strategies that modified the text.
    """

    original_tokens: int
    compacted_tokens: int
    strategy_used: str


# ---------------------------------------------------------------------------
# Individual compaction strategies — sourced from bernstein.core.defaults.TOKEN
# ---------------------------------------------------------------------------

#: Maximum lines in a fenced code block before it is truncated.
_CODE_BLOCK_MAX_LINES: int = TOKEN.code_block_max_lines

#: Maximum file listing entries to keep.
_FILE_LISTING_MAX_ENTRIES: int = TOKEN.file_listing_max_entries

#: Regex for fenced code blocks (``` ... ```).
_FENCED_CODE_RE = re.compile(
    r"(```[^\n]*\n)(.*?)(```)",
    re.DOTALL,
)

#: Regex for traceback blocks (Python-style).
_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?)(?=\n\n|\Z)",
    re.DOTALL,
)


def _truncate_large_code_blocks(text: str) -> tuple[str, bool]:
    """Replace fenced code blocks exceeding ``_CODE_BLOCK_MAX_LINES`` with a summary.

    Args:
        text: Input prompt text.

    Returns:
        Tuple of (modified text, whether any block was truncated).
    """
    changed = False

    def _replacer(match: re.Match[str]) -> str:
        nonlocal changed
        opener = match.group(1)
        body = match.group(2)
        closer = match.group(3)
        lines = body.splitlines()
        if len(lines) > _CODE_BLOCK_MAX_LINES:
            changed = True
            kept = "\n".join(lines[:10])
            return f"{opener}{kept}\n# ... ({len(lines) - 10} more lines truncated, {len(lines)} total)\n{closer}"
        return match.group(0)

    result = _FENCED_CODE_RE.sub(_replacer, text)
    return result, changed


def _remove_duplicate_sections(text: str) -> tuple[str, bool]:
    """Remove duplicate paragraph-level sections from the prompt.

    Two consecutive blank-line-separated blocks with identical content are
    collapsed to a single occurrence plus a note.

    Args:
        text: Input prompt text.

    Returns:
        Tuple of (deduplicated text, whether any duplicates were removed).
    """
    paragraphs = re.split(r"\n{2,}", text)
    seen: set[str] = set()
    deduped: list[str] = []
    removed_count = 0

    for para in paragraphs:
        normalized = para.strip()
        if not normalized:
            continue
        if normalized in seen:
            removed_count += 1
            continue
        seen.add(normalized)
        deduped.append(para)

    if removed_count > 0:
        deduped.append(f"[{removed_count} duplicate section(s) removed]")
        return "\n\n".join(deduped), True
    return text, False


def _truncate_file_listings(text: str) -> tuple[str, bool]:
    """Truncate file listing blocks to the first ``_FILE_LISTING_MAX_ENTRIES`` entries.

    Detects patterns like consecutive lines that look like file paths
    (starting with ``./``, ``/``, or indented with common tree characters).

    Args:
        text: Input prompt text.

    Returns:
        Tuple of (modified text, whether any listing was truncated).
    """
    # Match blocks of lines that look like file paths
    file_line_re = re.compile(r"^[\s]*(?:[./]|[|`\\]|[a-zA-Z]:[\\/]).*$")

    lines = text.split("\n")
    result_lines: list[str] = []
    listing_lines: list[str] = []
    changed = False

    def _flush_listing() -> None:
        nonlocal changed
        if len(listing_lines) > _FILE_LISTING_MAX_ENTRIES:
            changed = True
            result_lines.extend(listing_lines[:_FILE_LISTING_MAX_ENTRIES])
            result_lines.append(
                f"# ... ({len(listing_lines) - _FILE_LISTING_MAX_ENTRIES} "
                f"more entries truncated, {len(listing_lines)} total)"
            )
        else:
            result_lines.extend(listing_lines)

    for line in lines:
        if file_line_re.match(line):
            listing_lines.append(line)
        else:
            if listing_lines:
                _flush_listing()
                listing_lines = []
            result_lines.append(line)

    if listing_lines:
        _flush_listing()

    return "\n".join(result_lines), changed


def _strip_verbose_tracebacks(text: str) -> tuple[str, bool]:
    """Keep only the last frame of Python tracebacks.

    Replaces full multi-frame tracebacks with the final ``File "..."`` line
    and the exception message, reducing noise while preserving the root cause.

    Args:
        text: Input prompt text.

    Returns:
        Tuple of (modified text, whether any traceback was stripped).
    """
    changed = False

    def _replacer(match: re.Match[str]) -> str:
        nonlocal changed
        full_tb = match.group(0)
        lines = full_tb.splitlines()
        if len(lines) <= 4:
            # Short tracebacks are already compact
            return full_tb

        changed = True
        # Find the last "File ..." line and everything after it
        last_frame_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("File "):
                last_frame_idx = i
                break

        if last_frame_idx >= 0:
            kept = [
                "Traceback (most recent call last):",
                f"  # ... ({last_frame_idx - 1} frames omitted)",
                *lines[last_frame_idx:],
            ]
            return "\n".join(kept)
        # Fallback: keep header + last 2 lines
        return "\n".join([lines[0], "  # ... (frames omitted)", *lines[-2:]])

    result = _TRACEBACK_RE.sub(_replacer, text)
    return result, changed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token for English text."""
    return max(1, len(text) // 4)


def compact_prompt(
    prompt: str,
    max_reduction: float = 0.5,
) -> tuple[str, CompactionResult]:
    """Apply heuristic compaction strategies to reduce prompt size.

    Applies strategies in order until the cumulative reduction reaches
    ``max_reduction`` (as a fraction of the original token count), or all
    strategies have been attempted.

    Strategies applied:
    1. Truncate large code blocks (>100 lines).
    2. Remove duplicate context sections.
    3. Truncate file listings to first 50 entries.
    4. Strip verbose error tracebacks (keep last frame).

    Args:
        prompt: The full prompt text to compact.
        max_reduction: Maximum fraction of tokens to remove (0.0-1.0).
            Compaction stops early if this threshold is reached.

    Returns:
        Tuple of (compacted prompt text, CompactionResult with metadata).
    """
    original_tokens = _estimate_tokens(prompt)
    target_tokens = int(original_tokens * (1.0 - max_reduction))
    strategies_used: list[str] = []
    working = prompt

    # Strategy 1: Truncate large code blocks
    working, did_change = _truncate_large_code_blocks(working)
    if did_change:
        strategies_used.append("truncate_code_blocks")
    if _estimate_tokens(working) <= target_tokens:
        return _build_result(prompt, working, original_tokens, strategies_used)

    # Strategy 2: Remove duplicate sections
    working, did_change = _remove_duplicate_sections(working)
    if did_change:
        strategies_used.append("remove_duplicates")
    if _estimate_tokens(working) <= target_tokens:
        return _build_result(prompt, working, original_tokens, strategies_used)

    # Strategy 3: Truncate file listings
    working, did_change = _truncate_file_listings(working)
    if did_change:
        strategies_used.append("truncate_file_listings")
    if _estimate_tokens(working) <= target_tokens:
        return _build_result(prompt, working, original_tokens, strategies_used)

    # Strategy 4: Strip verbose tracebacks
    working, did_change = _strip_verbose_tracebacks(working)
    if did_change:
        strategies_used.append("strip_tracebacks")

    return _build_result(prompt, working, original_tokens, strategies_used)


def _build_result(
    original: str,
    compacted: str,
    original_tokens: int,
    strategies: list[str],
) -> tuple[str, CompactionResult]:
    """Build the return tuple for ``compact_prompt``.

    Args:
        original: Original prompt text (unused, kept for signature clarity).
        compacted: Compacted prompt text.
        original_tokens: Pre-computed original token estimate.
        strategies: List of strategy names that were applied.

    Returns:
        Tuple of (compacted text, CompactionResult).
    """
    compacted_tokens = _estimate_tokens(compacted)
    return compacted, CompactionResult(
        original_tokens=original_tokens,
        compacted_tokens=compacted_tokens,
        strategy_used=", ".join(strategies) if strategies else "none",
    )


def should_compact(log_path: Path) -> bool:
    """Scan an agent log file for 413 / context overflow patterns.

    Reads the last 500 lines of the log and checks for any of the known
    context-overflow indicators (HTTP 413, prompt-too-long messages, etc.).

    Delegates to :meth:`RateLimitTracker.scan_log_for_context_overflow` which
    uses the canonical ``_CONTEXT_OVERFLOW_PATTERNS`` tuple.

    Args:
        log_path: Path to the agent's subprocess log file.

    Returns:
        True if context overflow indicators were found, False otherwise
        (including when the file does not exist or cannot be read).
    """
    tracker = RateLimitTracker()
    return tracker.scan_log_for_context_overflow(log_path)
