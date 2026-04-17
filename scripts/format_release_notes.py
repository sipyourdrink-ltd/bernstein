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
_PR_SUFFIX_RE = re.compile(r"\s*\(#\d+\)\s*$")
_CC_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?!?:\s*(?P<subject>.+)$")


def format_notes(version: str, prev_tag: str, repo: str, commits: list[str]) -> str:
    """Return the markdown release body for ``version``."""
    buckets: dict[str, list[str]] = {k: [] for k in _CATEGORIES}
    other: list[str] = []

    for raw in commits:
        subject = raw.strip()
        if not subject or any(subject.startswith(p) for p in _SKIP_PREFIXES):
            continue
        match = _CC_RE.match(subject)
        if not match:
            other.append(subject)
            continue
        commit_type = match.group("type")
        scope = match.group("scope") or ""
        text = _PR_SUFFIX_RE.sub("", match.group("subject").strip())
        bullet = f"**{scope}:** {text}" if scope else text
        if commit_type in buckets:
            buckets[commit_type].append(bullet)
        else:
            other.append(subject)

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

    return "\n".join(out).rstrip() + "\n"


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
