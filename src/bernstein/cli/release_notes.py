"""Release notes display - fetch and format release notes for terminal output.

Release history lives in ``docs/release-notes/``, one ``vX.Y.Z.md`` page per
tagged version. ``fetch_release_notes()`` retrieves the page for the installed
version from a remote URL with a timeout, falling back to the newest page in a
local ``docs/release-notes/`` directory when the network is unavailable.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Maintained release-notes surface, relative to the project root.
_RELEASE_NOTES_DIR = Path("docs") / "release-notes"

# ``vX.Y.Z.md`` only: anything else in the directory (``unreleased.md``, an
# index page) is not a released version and must not win the "newest" pick.
_VERSION_FILE_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.md$")

_RAW_BASE_URL = "https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/docs/release-notes"

# Last-resort local reads when no release-notes directory is present. Both are
# pointer documents that name the maintained surface.
_LOCAL_PATHS = ["CHANGELOG.md", "docs/CHANGELOG.md", "README.md"]

_TIMEOUT_S = 2.0  # 500ms would be too aggressive for GitHub; 2s is reasonable


def installed_version() -> str:
    """Return the installed package version.

    Returns:
        The version string from the package metadata, e.g. ``"3.12.0"``.
    """
    from bernstein import __version__

    return __version__


def release_notes_url(version: str | None = None) -> str:
    """Build the raw URL of the release-notes page for a version.

    Args:
        version: Version to look up. Defaults to the installed version.

    Returns:
        Raw content URL for that version's release-notes page.
    """
    return f"{_RAW_BASE_URL}/v{version or installed_version()}.md"


def latest_release_notes_path(workdir: Path) -> Path | None:
    """Find the newest versioned release-notes page under ``workdir``.

    The pick is deterministic: files are ordered by their parsed
    ``(major, minor, patch)`` tuple, so ``v3.12.0`` sorts above ``v3.9.0``
    rather than below it as a lexicographic sort would.

    Args:
        workdir: Project root holding ``docs/release-notes/``.

    Returns:
        Path to the highest-versioned page, or None if there is none.
    """
    notes_dir = workdir / _RELEASE_NOTES_DIR
    if not notes_dir.is_dir():
        return None

    best: tuple[tuple[int, int, int], Path] | None = None
    for entry in sorted(notes_dir.iterdir()):
        match = _VERSION_FILE_RE.match(entry.name)
        if match is None or not entry.is_file():
            continue
        key = (int(match[1]), int(match[2]), int(match[3]))
        if best is None or key > best[0]:
            best = (key, entry)
    return None if best is None else best[1]


def fetch_release_notes(
    url: str | None = None,
    workdir: Path | None = None,
) -> str:
    """Fetch release notes, falling back to local files on failure.

    Args:
        url: Remote URL to fetch release notes from. Defaults to the page for
            the installed version.
        workdir: Project root for the local fallback.

    Returns:
        Release-notes text, or an error message if unavailable.
    """
    remote_url = url or release_notes_url()
    # Try remote fetch
    content = _fetch_remote(remote_url)
    if content is not None:
        return content

    # Fallback to local files
    local = _find_local_changelog(workdir or Path.cwd())
    if local is not None:
        return local

    return "Changelog not available. Visit https://github.com/sipyourdrink-ltd/bernstein/releases for release notes."


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
            logger.debug("Remote release notes HTTP %d from %s", resp.status_code, url)
            return None
    except Exception as exc:
        logger.debug("Remote release notes fetch failed: %s", exc)
        return None


def _find_local_changelog(workdir: Path) -> str | None:
    """Read the newest local release-notes page, or a pointer document.

    Args:
        workdir: Project root directory.

    Returns:
        File contents, or None if nothing readable was found.
    """
    latest = latest_release_notes_path(workdir)
    if latest is not None:
        try:
            return latest.read_text(encoding="utf-8")
        except OSError:
            logger.debug("Local release notes unreadable: %s", latest)

    for rel in _LOCAL_PATHS:
        fpath = workdir / rel
        if fpath.exists():
            try:
                return fpath.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def format_for_terminal(raw: str, max_lines: int = 100) -> str:
    """Format release-notes text for terminal display.

    Wraps the raw text, limiting to a reasonable number of lines
    and converting markdown headings to terminal-friendly markers.

    Args:
        raw: Raw release-notes markdown text.
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
