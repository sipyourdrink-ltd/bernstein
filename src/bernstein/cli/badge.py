"""Powered-by badge helper for ``bernstein init --add-badge``.

The shields.io static-badge API (https://shields.io/badges/static-badge) is
served from CDN-cached templates rendered at request time, so each variant
is a permanent URL that costs the operator nothing.

Variants
--------

We expose four restrained labels so a downstream maintainer can pick the
phrasing that least bruises their README aesthetic:

* ``signed``           - emphasises the HMAC audit-chain
* ``audited-by``       - emphasises post-merge inspection
* ``orchestrated-by``  - emphasises the multi-agent workflow
* ``crew-managed-by``  - emphasises the parallel CLI-agent crew

Behaviour contract
------------------

The injection helper is conservative: it never duplicates a badge that is
already present, never reorders an existing badge stack, and never rewrites
content outside the badge block at the top of the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Detector for shields.io badge image references in markdown.
# This is **not** URL sanitization - we are looking for the markdown image
# pattern `![alt](https://img.shields.io/...)` so we can append a new badge
# to the END of the existing stack.  The regex anchors on the canonical
# domain pieces so an unrelated string like "shields.io.example.com" cannot
# match.  Codepath only reads the maintainer's own README and never makes
# a security decision based on the result.
_BADGE_URL_PATTERN = re.compile(
    r"!\[[^\]]*\]\(https?://(?:img\.)?shields\.io/",
    re.IGNORECASE,
)

# UTM params let bernstein.run measure inbound badge clicks without
# touching the maintainer's analytics layer.
_DEFAULT_LINK = "https://bernstein.run/?utm_source=badge&utm_medium=readme&utm_campaign=powered-by"

# Shields static-badge URL.  ``message`` is the always-bold-right-side
# label; ``label`` is the always-grey-left-side label.  ``color`` is a
# hex without ``#``.  ``logo`` references one of shields' built-in slugs.
_SHIELDS_TEMPLATE = (
    "https://img.shields.io/badge/{label}-{message}-{color}?logo=githubactions&logoColor=white&style=flat-square"
)


@dataclass(frozen=True)
class BadgeVariant:
    """One shields.io static-badge variant."""

    name: str
    label: str
    message: str
    color: str
    alt_text: str

    def url(self) -> str:
        """Return the shields.io image URL for this variant."""
        return _SHIELDS_TEMPLATE.format(
            label=self.label.replace(" ", "_").replace("-", "--"),
            message=self.message.replace(" ", "_").replace("-", "--"),
            color=self.color,
        )

    def markdown(self, *, link: str = _DEFAULT_LINK) -> str:
        """Render the markdown ``[![alt](src)](href)`` snippet."""
        return f"[![{self.alt_text}]({self.url()})]({link})"


_VARIANTS: tuple[BadgeVariant, ...] = (
    BadgeVariant(
        name="signed",
        label="signed_by",
        message="bernstein",
        color="FBBF24",
        alt_text="signed by bernstein",
    ),
    BadgeVariant(
        name="audited-by",
        label="audited_by",
        message="bernstein",
        color="FBBF24",
        alt_text="audited by bernstein",
    ),
    BadgeVariant(
        name="orchestrated-by",
        label="orchestrated_by",
        message="bernstein",
        color="FBBF24",
        alt_text="orchestrated by bernstein",
    ),
    BadgeVariant(
        name="crew-managed-by",
        label="crew_managed_by",
        message="bernstein",
        color="FBBF24",
        alt_text="crew managed by bernstein",
    ),
)


def list_variants() -> tuple[BadgeVariant, ...]:
    """Return every badge variant in deterministic order."""
    return _VARIANTS


def get_variant(name: str) -> BadgeVariant:
    """Look up a variant by its short name; raises ``KeyError`` on miss."""
    for variant in _VARIANTS:
        if variant.name == name:
            return variant
    raise KeyError(f"unknown badge variant: {name!r}")


_BADGE_URL_TOKENS: tuple[str, ...] = (
    "/signed_by-bernstein",
    "/audited_by-bernstein",
    "/orchestrated_by-bernstein",
    "/crew_managed_by-bernstein",
)


def _badge_already_present(readme_text: str) -> bool:
    """Detect whether any bernstein powered-by badge is already in the README."""
    haystack = readme_text.lower()
    if "shields.io/badge" not in haystack:
        return False
    if "bernstein" not in haystack:
        return False
    # Conservative: only treat it as "already present" when the badge image
    # URL itself contains ``bernstein``, not just a link target.
    return any(token in haystack for token in _BADGE_URL_TOKENS)


def _insertion_index(readme_text: str) -> int:
    """Return the byte offset where the new badge line should be inserted.

    Strategy:

    1. If a badge stack exists in the first 30 lines, append to the END of
       that stack (preserve maintainer's chosen ordering).
    2. Otherwise, insert immediately after the H1 line at the top.
    3. Otherwise, prepend to the file.
    """
    lines = readme_text.splitlines(keepends=True)
    head = lines[:30]

    last_badge_line = -1
    for idx, raw in enumerate(head):
        if _BADGE_URL_PATTERN.search(raw):
            last_badge_line = idx
    if last_badge_line >= 0:
        return sum(len(line) for line in lines[: last_badge_line + 1])

    for idx, raw in enumerate(head):
        if raw.lstrip().startswith("# "):
            return sum(len(line) for line in lines[: idx + 1])

    return 0


def inject_badge(
    readme_path: Path,
    variant: BadgeVariant,
    *,
    link: str = _DEFAULT_LINK,
) -> bool:
    """Insert *variant*'s markdown line into *readme_path*.

    Returns ``True`` when the file was modified, ``False`` when the badge
    was already present (idempotent) or the file is missing.
    """
    if not readme_path.exists():
        return False

    # Decode the bytes as they are: ``read_text`` translates CRLF to LF and
    # ``write_text`` then wrote LF over every existing line (#8059).
    text = readme_path.read_bytes().decode("utf-8")
    if _badge_already_present(text):
        return False

    snippet = variant.markdown(link=link)
    insert_at = _insertion_index(text)
    prefix = text[:insert_at]
    suffix = text[insert_at:]

    terminator = _line_terminator(prefix, suffix)
    needs_leading_nl = bool(prefix) and not prefix.endswith(("\n", "\r"))
    glue = (terminator if needs_leading_nl else "") + snippet + terminator
    readme_path.write_bytes((prefix + glue + suffix).encode("utf-8"))
    return True


def _line_terminator(prefix: str, suffix: str) -> str:
    """Return the line terminator in use at the insertion point.

    The line before the insertion point decides. With no terminated line
    before it, the first line after it decides, then LF.
    """
    for ending in ("\r\n", "\n", "\r"):
        if prefix.endswith(ending):
            return ending
    match = re.search(r"\r\n|\n|\r", suffix)
    return match.group(0) if match else "\n"
