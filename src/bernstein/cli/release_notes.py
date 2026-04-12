"""Release notes display — fetch and format CHANGELOG.md for terminal output.

Provides ``fetch_release_notes()`` that retrieves the project changelog
from a remote URL with timeout, falling back to a local CHANGELOG.md file
if the network is unavailable.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default remote changelog URL for Bernstein
_DEFAULT_CHANGELOG_URL = "https://raw.githubusercontent.com/chernistry/bernstein/main/CHANGELOG.md"

# Local fallback paths (relative to project root)
_LOCAL_PATHS = ["CHANGELOG.md", "README.md"]

_TIMEOUT_S = 2.0  # 500ms would be too aggressive for GitHub; 2s is reasonable


def fetch_release_notes(
    url: str | None = None,
    workdir: Path | None = None,
) -> str:
    """Fetch release notes, falling back to local file on failure.

    Args:
        url: Remote URL to fetch CHANGELOG.md from.
            Defaults to Bernstein's GitHub raw changelog.
        workdir: Project root for local file fallback.

    Returns:
        Changelog text, or an error message if unavailable.
    """
    remote_url = url or _DEFAULT_CHANGELOG_URL
    # Try remote fetch
    content = _fetch_remote(remote_url)
    if content is not None:
        return content

    # Fallback to local file
    local = _find_local_changelog(workdir or Path.cwd())
    if local is not None:
        return local

    return "Changelog not available. Visit https://github.com/chernistry/bernstein/releases for release notes."


def _fetch_remote(url: str) -> str | None:
    """Fetch text from a URL with timeout.

    Args:
        url: URL to fetch.

    Returns:
        Response text, or None on failure.
    """
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT_S) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.text
            logger.debug("Remote changelog HTTP %d from %s", resp.status_code, url)
            return None
    except Exception as exc:
        logger.debug("Remote changelog fetch failed: %s", exc)
        return None


def _find_local_changelog(workdir: Path) -> str | None:
    """Look for a local changelog file.

    Args:
        workdir: Project root directory.

    Returns:
        File contents, or None if not found.
    """
    for rel in _LOCAL_PATHS:
        fpath = workdir / rel
        if fpath.exists():
            try:
                return fpath.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def format_for_terminal(raw: str, max_lines: int = 100) -> str:
    """Format changelog text for terminal display.

    Wraps the raw text, limiting to a reasonable number of lines
    and converting markdown headings to terminal-friendly markers.

    Args:
        raw: Raw changelog markdown text.
        max_lines: Maximum lines to return.

    Returns:
        Formatted string suitable for console printing.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        # Strip markdown heading markers, keep the text
        stripped = re.sub(r"^#+\s*", "", line)
        if stripped.strip():
            lines.append(stripped.rstrip())
        if len(lines) >= max_lines:
            lines.append("... (truncated)")
            break
    return "\n".join(lines)
