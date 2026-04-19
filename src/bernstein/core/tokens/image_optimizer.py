"""Agent context image optimization.

CLI agents that use a ``Read`` tool to view screenshots or images end up
with base64 blobs persisted in their conversation history.  Every
subsequent API call re-sends those blobs, burning tokens on data the
agent no longer needs.

This module provides utilities to:

- Estimate the token cost of base64 image data.
- Strip images older than N turns from a conversation message list.
- Scan agent session directories for accumulated image waste.
- Render a Markdown report of cleanup results.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Approximate bytes-per-token ratio for base64 image data.
_BYTES_PER_TOKEN: int = 4

#: Regex matching base64 data URIs (e.g. ``data:image/png;base64,...``).
_BASE64_DATA_URI_RE: re.Pattern[str] = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]{100,}")

#: Regex matching raw base64 blobs (long runs of base64 chars).
_RAW_BASE64_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9+/=]{500,}")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageCleanupResult:
    """Result of cleaning images from agent context.

    Attributes:
        images_found: Total number of image content blocks found.
        images_removed: Number of image blocks actually removed.
        tokens_saved_estimate: Estimated tokens freed by removal.
        kept_recent: Number of images kept (within *keep_last* window).
    """

    images_found: int
    images_removed: int
    tokens_saved_estimate: int
    kept_recent: int


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def estimate_image_tokens(base64_data: str) -> int:
    """Estimate the token cost of a base64-encoded image.

    Uses a rough heuristic of ~1 token per 4 bytes of base64 data.

    Args:
        base64_data: Raw base64 string (without data-URI prefix).

    Returns:
        Estimated token count.
    """
    return len(base64_data) // _BYTES_PER_TOKEN


def should_strip_image(image_age_turns: int, keep_last: int = 2) -> bool:
    """Decide whether an image should be stripped based on age.

    Args:
        image_age_turns: How many conversation turns ago the image appeared.
        keep_last: Number of recent turns whose images are preserved.

    Returns:
        ``True`` if the image is old enough to strip.
    """
    return image_age_turns > keep_last


def clean_images_from_context(
    messages: list[dict[str, Any]],
    keep_last: int = 2,
) -> ImageCleanupResult:
    """Remove old base64 images from conversation messages.

    Images read via the Read tool persist as base64 in conversation
    history and are sent on every subsequent API call.  This strips
    images older than *keep_last* turns to reduce token waste.

    The function mutates *messages* in place and returns a summary.

    Args:
        messages: List of conversation message dicts.  Each message may
            have a ``content`` field that is either a string or a list of
            content blocks (dicts with ``"type"`` keys).
        keep_last: Number of most-recent turns whose images are kept.

    Returns:
        An :class:`ImageCleanupResult` summarising the cleanup.
    """
    total_messages = len(messages)
    images_found = 0
    images_removed = 0
    tokens_saved = 0
    kept_recent = 0

    for idx, msg in enumerate(messages):
        age = total_messages - 1 - idx
        content = msg.get("content")
        if content is None:
            continue

        # Content can be a plain string or a list of content blocks.
        if isinstance(content, str):
            continue

        if not isinstance(content, list):
            continue

        typed_content = cast(list[dict[str, Any]], content)
        blocks_to_keep: list[dict[str, Any]] = []
        for block in typed_content:
            block_type: str = str(block.get("type", ""))
            if block_type in ("image", "image_url"):
                images_found += 1
                image_data = _extract_base64(block)
                if should_strip_image(age, keep_last):
                    tokens_saved += estimate_image_tokens(image_data)
                    images_removed += 1
                    # Replace with a placeholder text block.
                    blocks_to_keep.append({"type": "text", "text": "[image removed to save tokens]"})
                else:
                    kept_recent += 1
                    blocks_to_keep.append(block)
            else:
                blocks_to_keep.append(block)

        msg["content"] = blocks_to_keep

    return ImageCleanupResult(
        images_found=images_found,
        images_removed=images_removed,
        tokens_saved_estimate=tokens_saved,
        kept_recent=kept_recent,
    )


def scan_for_image_waste(session_dir: Path) -> dict[str, Any]:
    """Scan agent session files for accumulated image data.

    Checks ``.sdd/runtime/*.log`` for base64 patterns and estimates how
    many tokens are being wasted on stale image data.

    Args:
        session_dir: Path to a ``.sdd/runtime/`` directory (or similar).

    Returns:
        Dict with keys ``total_image_bytes``, ``estimated_tokens``,
        ``files_scanned``, and ``recommendation``.
    """
    total_bytes = 0
    files_scanned = 0

    if not session_dir.is_dir():
        return {
            "total_image_bytes": 0,
            "estimated_tokens": 0,
            "files_scanned": 0,
            "recommendation": "Directory does not exist.",
        }

    for log_file in session_dir.glob("*.log"):
        files_scanned += 1
        try:
            text = log_file.read_text(errors="replace")
        except OSError:
            logger.debug("Could not read %s", log_file, exc_info=True)
            continue

        for match in _BASE64_DATA_URI_RE.finditer(text):
            total_bytes += len(match.group())
        for match in _RAW_BASE64_RE.finditer(text):
            total_bytes += len(match.group())

    estimated_tokens = total_bytes // _BYTES_PER_TOKEN

    if estimated_tokens > 50_000:
        recommendation = "High image waste detected. Run image cleanup before next session."
    elif estimated_tokens > 10_000:
        recommendation = "Moderate image waste. Consider cleanup on next session restart."
    else:
        recommendation = "Image waste is minimal."

    return {
        "total_image_bytes": total_bytes,
        "estimated_tokens": estimated_tokens,
        "files_scanned": files_scanned,
        "recommendation": recommendation,
    }


def render_image_report(result: ImageCleanupResult) -> str:
    """Render a Markdown report of image cleanup results.

    Args:
        result: The cleanup result to format.

    Returns:
        Markdown-formatted string.
    """
    lines = [
        "## Image Cleanup Report",
        "",
        f"- **Images found:** {result.images_found}",
        f"- **Images removed:** {result.images_removed}",
        f"- **Tokens saved (est.):** {result.tokens_saved_estimate:,}",
        f"- **Images kept (recent):** {result.kept_recent}",
    ]
    if result.images_removed == 0 and result.images_found > 0:
        lines.append("")
        lines.append("All images are within the keep window; nothing to clean.")
    elif result.images_found == 0:
        lines.append("")
        lines.append("No images found in context.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_base64(block: dict[str, Any]) -> str:
    """Extract the base64 payload from an image content block.

    Handles both Anthropic-style (``source.data``) and OpenAI-style
    (``image_url.url``) image blocks.

    Args:
        block: A content block dict.

    Returns:
        The raw base64 string, or ``""`` if not found.
    """
    # Anthropic format: {"type": "image", "source": {"data": "..."}}
    source: Any = block.get("source")
    if isinstance(source, dict):
        data: Any = cast(dict[str, Any], source).get("data", "")
        if isinstance(data, str):
            return data

    # OpenAI format: {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
    image_url_val: Any = block.get("image_url")
    if isinstance(image_url_val, dict):
        url: Any = cast(dict[str, Any], image_url_val).get("url", "")
        if isinstance(url, str) and ";base64," in url:
            return url.split(";base64,", 1)[1]

    return ""
