"""Contextual help system with inline documentation links.

Maps common error patterns to relevant documentation sections so that
users get actionable "See: <url>" pointers appended to error messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOCS_BASE = "https://bernstein.readthedocs.io/en/latest"

_ADAPTER_GUIDE = f"{_DOCS_BASE}/adapter-guide"


@dataclass(frozen=True)
class HelpLink:
    """A mapping from an error pattern to a documentation URL.

    Attributes:
        error_pattern: Regex pattern matched against error messages.
        doc_section: Documentation section path (appended to base URL).
        url: Full documentation URL for the matched error.
        summary: Short human-readable description of the linked docs.
    """

    error_pattern: str
    doc_section: str
    url: str
    summary: str


# ---------------------------------------------------------------------------
# Common error -> doc link mapping
# ---------------------------------------------------------------------------

HELP_LINKS: list[HelpLink] = [
    HelpLink(
        error_pattern=r"(?i)rate.limit",
        doc_section="troubleshooting#rate-limits",
        url=f"{_DOCS_BASE}/troubleshooting#rate-limits",
        summary="Troubleshooting API rate limits",
    ),
    HelpLink(
        error_pattern=r"(?i)spawn.*fail",
        doc_section="troubleshooting#spawn-failures",
        url=f"{_DOCS_BASE}/troubleshooting#spawn-failures",
        summary="Troubleshooting agent spawn failures",
    ),
    HelpLink(
        error_pattern=r"(?i)budget.*exceed",
        doc_section="cost-optimization#budgets",
        url=f"{_DOCS_BASE}/cost-optimization#budgets",
        summary="Configuring and managing budgets",
    ),
    HelpLink(
        error_pattern=r"(?i)merge.*conflict",
        doc_section="troubleshooting#merge-conflicts",
        url=f"{_DOCS_BASE}/troubleshooting#merge-conflicts",
        summary="Resolving merge conflicts",
    ),
    HelpLink(
        error_pattern=r"(?i)worktree.*lock",
        doc_section="troubleshooting#git-locks",
        url=f"{_DOCS_BASE}/troubleshooting#git-locks",
        summary="Clearing git worktree locks",
    ),
    HelpLink(
        error_pattern=r"(?i)adapter.*not.found",
        doc_section="adapter-guide",
        url=_ADAPTER_GUIDE,
        summary="Installing and configuring adapters",
    ),
    HelpLink(
        error_pattern=r"(?i)permission.*denied",
        doc_section="security-hardening",
        url=f"{_DOCS_BASE}/security-hardening",
        summary="Security hardening and permissions",
    ),
    HelpLink(
        error_pattern=r"(?i)timeout",
        doc_section="performance-tuning#timeouts",
        url=f"{_DOCS_BASE}/performance-tuning#timeouts",
        summary="Performance tuning and timeout configuration",
    ),
]

# Compiled patterns for faster matching
_COMPILED: list[tuple[re.Pattern[str], HelpLink]] = [(re.compile(link.error_pattern), link) for link in HELP_LINKS]


def find_help_link(error_message: str) -> HelpLink | None:
    """Match an error message against known patterns and return the help link.

    Args:
        error_message: The error message string to match.

    Returns:
        The first matching ``HelpLink``, or ``None`` if no pattern matches.
    """
    for compiled_pattern, link in _COMPILED:
        if compiled_pattern.search(error_message):
            return link
    return None


def format_help_suggestion(link: HelpLink) -> str:
    """Format a help link as a user-facing suggestion string.

    Args:
        link: The help link to format.

    Returns:
        A string like ``"See: https://bernstein.readthedocs.io/en/latest/<section>"``.
    """
    return f"See: {link.url}"


def enrich_error_message(error: str) -> str:
    """Append a documentation link to an error message if a pattern matches.

    If the error matches a known pattern, the formatted help suggestion is
    appended on a new line.  Otherwise the error is returned unchanged.

    Args:
        error: The original error message.

    Returns:
        The error message, possibly enriched with a documentation link.
    """
    link = find_help_link(error)
    if link is None:
        return error
    return f"{error}\n{format_help_suggestion(link)}"
