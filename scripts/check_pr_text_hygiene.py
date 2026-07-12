#!/usr/bin/env python
"""Pre-merge lint that scans pull-request text against a deny-list.

The script reads four surfaces of a pull request:

  - ``--title`` (string)
  - ``--body`` (string; markdown is fine, only substring scanning is done)
  - ``--branch`` (string; the head ref name)
  - ``--commit-messages-file`` (path to a file containing every commit
    subject + body in the PR, concatenated; the ``git log %B`` output
    with ``---`` separators is the expected format)

The deny-list source is configurable:

  - ``--denylist-env-var NAME`` reads phrases from the named environment
    variable. Value is either a JSON object ``{"denylist": [...]}`` or
    a plain newline-separated list. This is the recommended source so
    the phrase list never lands in the repo.
  - ``--denylist PATH`` reads phrases from a JSON file (same schema as
    the env-var variant). Useful for local runs.

When neither source resolves to a non-empty phrase list the script logs
a notice and exits 0 (the workflow becomes a no-op). Matching is plain
case-insensitive substring matching against each surface. Any match is
reported via a GitHub Actions ``::error::`` annotation on stdout and
the script exits with status 1; a clean run exits 0.

The script never reads PR labels. Label-based opt-out lives in the
workflow that calls this script (see
``.github/workflows/pr-text-hygiene.yml``).

Run locally::

    PR_TEXT_HYGIENE_DENYLIST='{"denylist":["foo","bar"]}' \\
      python scripts/check_pr_text_hygiene.py \\
      --title "ci: add foo" \\
      --body "" \\
      --branch "feat/foo" \\
      --commit-messages-file commit-msgs.txt \\
      --denylist-env-var PR_TEXT_HYGIENE_DENYLIST
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Phrases at or below this length are treated as short tokens (e.g. a
# three-letter acronym). Short tokens require a non-alphanumeric boundary
# on BOTH sides so they do not match by accident inside an unrelated
# longer word. Longer phrases require only a left boundary so that
# suffixed forms (plurals, adjective endings) are still caught.
_SHORT_PHRASE_MAX_LEN = 4


def _parse_denylist_payload(raw: str, source: str) -> list[str]:
    """Parse a deny-list payload that may be JSON or newline-separated.

    JSON form: ``{"denylist": ["phrase-1", "phrase-2", ...]}``.
    Plain form: one phrase per line, blanks ignored.
    Returns the cleaned list of non-empty stripped phrases.
    """
    stripped = raw.strip()
    if not stripped:
        return []
    phrases: list[str]
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict) or "denylist" not in data:
            raise ValueError(f"deny-list source {source} missing top-level 'denylist' key")
        raw_phrases = data["denylist"]
        if not isinstance(raw_phrases, list):
            raise ValueError(f"deny-list source {source} 'denylist' must be a list")
        phrases = []
        for entry in raw_phrases:
            if not isinstance(entry, str):
                raise ValueError(f"deny-list source {source} contains non-string entry: {entry!r}")
            phrases.append(entry)
    else:
        phrases = stripped.splitlines()
    cleaned: list[str] = []
    for entry in phrases:
        normalised = entry.strip()
        if normalised:
            cleaned.append(normalised)
    return cleaned


def load_denylist(path: Path) -> list[str]:
    """Load a deny-list JSON file. Kept for tests and local invocations."""
    return _parse_denylist_payload(path.read_text(encoding="utf-8"), str(path))


def load_denylist_from_env(env_var: str) -> list[str]:
    """Load a deny-list from the named environment variable.

    Empty or missing env var resolves to an empty list (the caller can
    decide whether that is a no-op or an error).
    """
    raw = os.environ.get(env_var, "")
    return _parse_denylist_payload(raw, f"env:{env_var}")


@functools.lru_cache(maxsize=512)
def _compile_phrase(phrase: str) -> re.Pattern[str]:
    """Compile a deny-list phrase into a boundary-aware pattern.

    The pattern reduces false positives from plain substring matching:

    * A left boundary ``(?<![A-Za-z0-9])`` is always required, so a phrase
      never matches when it is glued to the tail of a longer word.
    * A right boundary ``(?![A-Za-z0-9])`` is added only for short phrases
      (``<= _SHORT_PHRASE_MAX_LEN``). Short acronyms are the main source of
      accidental hits inside longer words, so they must be delimited on
      both sides; longer phrases keep a left-only boundary so suffixed
      forms still match.

    Matching stays case-insensitive.
    """
    left = r"(?<![A-Za-z0-9])"
    right = r"(?![A-Za-z0-9])" if len(phrase) <= _SHORT_PHRASE_MAX_LEN else ""
    return re.compile(left + re.escape(phrase) + right, re.IGNORECASE)


def scan_surface(surface: str, text: str, phrases: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(surface, phrase)`` for each deny-list phrase that appears in *text*.

    Matching is case-insensitive and boundary-aware (see
    :func:`_compile_phrase`). ``text`` may be empty or whitespace only; in
    that case no matches are returned.
    """
    if not text:
        return []
    if not text.strip():
        return []
    findings: list[tuple[str, str]] = []
    for phrase in phrases:
        if _compile_phrase(phrase).search(text):
            findings.append((surface, phrase))
    return findings


def _read_commit_messages(path: Path) -> list[str]:
    """Split the commit-messages dump file into individual commit messages.

    The expected separator is a line containing only ``---``. Empty
    chunks are dropped.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    chunks = [chunk.strip() for chunk in text.split("\n---\n")]
    # Also tolerate a trailing standalone ``---`` token.
    cleaned: list[str] = []
    for chunk in chunks:
        stripped = chunk.strip().removesuffix("---").strip()
        if stripped:
            cleaned.append(stripped)
    return cleaned


def check_pr_text(
    title: str,
    body: str,
    branch: str,
    commit_messages: list[str],
    phrases: list[str],
) -> list[tuple[str, str]]:
    """Run the full scan across every PR surface; return all findings."""
    findings: list[tuple[str, str]] = []
    findings.extend(scan_surface("title", title, phrases))
    findings.extend(scan_surface("body", body, phrases))
    findings.extend(scan_surface("branch", branch, phrases))
    for idx, message in enumerate(commit_messages):
        findings.extend(scan_surface(f"commit[{idx}]", message, phrases))
    return findings


def _emit_finding(surface: str, phrase: str) -> None:
    """Print a GitHub Actions annotation for a single match."""
    print(f"::error file={surface}::{phrase} matched in {surface}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="", help="PR title (may be empty).")
    parser.add_argument("--body", default="", help="PR body markdown (may be empty).")
    parser.add_argument("--branch", default="", help="PR head branch name (may be empty).")
    parser.add_argument(
        "--commit-messages-file",
        type=Path,
        default=None,
        help="Path to a file with every commit subject + body separated by '---' lines.",
    )
    parser.add_argument(
        "--denylist",
        type=Path,
        default=None,
        help="Optional path to a deny-list JSON file. Prefer --denylist-env-var.",
    )
    parser.add_argument(
        "--denylist-env-var",
        default=None,
        help=(
            "Name of an environment variable whose value is the deny-list "
            "payload (JSON object with 'denylist' key, or newline-separated phrases)."
        ),
    )
    args = parser.parse_args(argv)

    phrases: list[str] = []
    if args.denylist_env_var:
        phrases = load_denylist_from_env(args.denylist_env_var)
    if not phrases and args.denylist is not None:
        phrases = load_denylist(args.denylist)
    if not phrases:
        print(
            "check_pr_text_hygiene: no deny-list configured; nothing to scan. "
            "Set --denylist-env-var or --denylist to enable the gate.",
        )
        return 0

    commit_messages: list[str] = []
    if args.commit_messages_file is not None:
        commit_messages = _read_commit_messages(args.commit_messages_file)

    findings = check_pr_text(
        title=args.title,
        body=args.body,
        branch=args.branch,
        commit_messages=commit_messages,
        phrases=phrases,
    )

    if not findings:
        print(f"check_pr_text_hygiene: OK ({len(phrases)} phrases, {len(commit_messages)} commit messages scanned)")
        return 0

    for surface, phrase in findings:
        _emit_finding(surface, phrase)
    print(
        f"check_pr_text_hygiene: FAIL ({len(findings)} match(es) across {len({s for s, _ in findings})} surface(s))",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
