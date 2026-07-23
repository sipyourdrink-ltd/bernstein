"""Format git commit subjects into categorised release notes.

Invoked by the auto-release workflow. Reads newline-separated commit
subjects from a file and groups them by conventional-commit prefix
(``feat:``, ``fix:``, ``refactor:``, etc.), producing a markdown
release body.

Usage:
    python scripts/format_release_notes.py \\
        --version 1.8.5 \\
        --prev-tag v1.8.4 \\
        --repo owner/name \\
        --commits /tmp/commits.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import OrderedDict
from pathlib import Path

_CATEGORIES: OrderedDict[str, str] = OrderedDict(
    [
        ("feat", "New features"),
        ("fix", "Bug fixes"),
        ("security", "Security"),
        ("perf", "Performance"),
        ("refactor", "Refactors & cleanup"),
        ("docs", "Documentation"),
        ("test", "Tests"),
        ("build", "Build / Packaging"),
        ("ci", "CI / Infrastructure"),
        ("style", "Style"),
        ("chore", "Chores"),
    ]
)

_SKIP_PREFIXES: tuple[str, ...] = ("chore: auto-bump",)
_BATCH_SUMMARY_PREFIXES: tuple[str, ...] = ("audit batch ", "batch ")
# Trailing PR ref, e.g. " (#123)". Inputs come from ``git log --pretty='%s'``
# so subjects are a single line - the explicit ``[^\n]`` bound and required
# single leading space avoid any quadratic-backtracking surface a static analyzer flags.
_PR_SUFFIX_RE = re.compile(r" \(#\d+\)$")
# Trailing parenthesised single internal-ticket ref, e.g. " (audit-123)".
# Single-pass only - for multi-ticket refs ``_strip_internal_refs`` loops.
# No nested repetition and no alternation under a quantifier, so this is
# linear-time regardless of input.
_TICKET_SUFFIX_RE = re.compile(r" \((?:audit|rt)-\d+\)$", re.IGNORECASE)
# Internal-ticket scope, e.g. "fix(audit-160): ..." - keep the subject,
# drop the scope so the reader sees "- ..." instead of "- **audit-160:** ...".
_TICKET_SCOPE_RE = re.compile(r"^(?:audit|rt)-\d+$", re.IGNORECASE)
_CC_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)\n]+)\))?!?: (?P<subject>[^\n]+)$")

# Linear-time URL matcher for bernstein.run links inside the rendered notes.
# Captures the host plus any path or query string up to the first whitespace
# or unbalanced ``)``. Bounded character class with no alternation under a
# quantifier - linear regardless of input.
_BERNSTEIN_URL_RE = re.compile(r"https://bernstein\.run[^\s)]*")


def _with_release_utm(body: str, version: str) -> str:
    """Append per-release UTM parameters to bernstein.run URLs in ``body``.

    Idempotent: a URL that already carries any ``utm_`` parameter is left
    untouched, so re-running on previously-tagged output does not double-tag.

    Args:
        body: Rendered markdown release-notes body.
        version: Release version string (e.g. ``"1.8.5"``); the leading
            ``v`` is stripped if present so the campaign reads
            ``v<MAJOR>.<MINOR>.<PATCH>`` regardless of caller convention.

    Returns:
        ``body`` with every bernstein.run URL UTM-tagged for this release.
    """
    clean_version = version[1:] if version.startswith("v") else version
    campaign = f"v{clean_version}"
    utm_params = f"utm_source=github.com&utm_medium=release-note&utm_campaign={campaign}"

    def _sub(match: re.Match[str]) -> str:
        url = match.group(0)
        if "utm_" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{utm_params}"

    return _BERNSTEIN_URL_RE.sub(_sub, body)


def _is_batch_summary(subject: str) -> bool:
    """Detect "Audit batch 6 (final): 9 tickets (142, 154, ...)" merge lines.

    Uses plain string ops (no regex) because the pattern is a fixed
    prefix followed by bounded keywords, and we want to keep the file
    free of static-analysis ReDoS hotspots.
    """
    lowered = subject.lower()
    if not lowered.startswith(_BATCH_SUMMARY_PREFIXES):
        return False
    colon = subject.find(":")
    if colon == -1:
        return False
    tail = subject[colon + 1 :].lstrip()
    # Expect: "<N> ticket[s] (…"
    count, _, rest = tail.partition(" ")
    if not count.isdigit():
        return False
    word, _, rest = rest.partition(" ")
    return word in ("ticket", "tickets") and rest.startswith("(")


def _strip_internal_refs(text: str) -> str:
    """Remove trailing internal-ticket refs like " (audit-123)" from a subject.

    Loops a linear-time regex for multi-ticket cases such as
    " (audit-123, audit-456)", stripping one ref per pass. Bounded by
    the subject length so termination is guaranteed.
    """
    result = text
    # Multi-ticket refs are rendered by git as " (audit-123, audit-456)";
    # strip the parentheses first when we see that shape, then let the
    # single-ref regex take over.
    while True:
        stripped = _TICKET_SUFFIX_RE.sub("", result)
        if stripped != result:
            result = stripped
            continue
        # Handle " (audit-123, audit-456[, ...])" by chopping one ref at a
        # time from the right inside the parentheses.
        if result.endswith(")") and ", " in result:
            open_paren = result.rfind("(")
            if open_paren == -1:
                break
            inside = result[open_paren + 1 : -1]
            refs = inside.split(", ")
            if all(_TICKET_SCOPE_RE.match(r) for r in refs):
                result = result[:open_paren].rstrip()
                continue
        break
    return result


def format_notes(version: str, prev_tag: str, repo: str, commits: list[str]) -> str:
    """Return the markdown release body for ``version``."""
    buckets: dict[str, list[str]] = {k: [] for k in _CATEGORIES}
    other: list[str] = []

    for raw in commits:
        subject = raw.strip()
        if not subject or any(subject.startswith(p) for p in _SKIP_PREFIXES):
            continue
        # Audit-batch merge summaries list internal ticket IDs with no
        # description of what actually changed. The real work shows up on
        # the per-fix commits, so drop the summary line entirely.
        if _is_batch_summary(subject):
            continue
        match = _CC_RE.match(subject)
        if not match:
            other.append(_strip_internal_refs(_PR_SUFFIX_RE.sub("", subject)))
            continue
        commit_type = match.group("type")
        scope = match.group("scope") or ""
        text = _strip_internal_refs(_PR_SUFFIX_RE.sub("", match.group("subject").strip()))
        # Ticket-only scopes (e.g. ``fix(audit-160): …``) add noise -
        # keep the fix description, drop the ticket scope marker.
        if _TICKET_SCOPE_RE.match(scope):
            scope = ""
        bullet = f"**{scope}:** {text}" if scope else text
        if commit_type in buckets:
            buckets[commit_type].append(bullet)
        else:
            other.append(bullet)

    out: list[str] = [f"## v{version}", ""]

    if not any(buckets.values()) and not other:
        out.append("_No user-visible changes._")

    for key, title in _CATEGORIES.items():
        items = buckets[key]
        if not items:
            continue
        out.append(f"### {title}")
        out.extend(f"- {b}" for b in items)
        out.append("")

    if other:
        out.append("### Other")
        out.extend(f"- {b}" for b in other)
        out.append("")

    if prev_tag:
        out.append(f"**Full changelog:** https://github.com/{repo}/compare/{prev_tag}...v{version}")

    body = "\n".join(out).rstrip() + "\n"
    return _with_release_utm(body, version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--prev-tag", default="")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commits", required=True, type=Path)
    args = parser.parse_args()

    commits = args.commits.read_text(encoding="utf-8").splitlines()
    sys.stdout.write(format_notes(args.version, args.prev_tag, args.repo, commits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
