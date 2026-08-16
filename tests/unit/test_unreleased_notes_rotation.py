"""``docs/release-notes/unreleased.md`` carries only work that has not shipped (#3788).

The page opens by promising it "carries what has landed since the newest one".
Nothing enforced that. It accumulated from 2026-05-18 until it listed twenty-eight
entries that had already gone out in v2.6.0 through v3.15.1, so a reader deciding
whether to upgrade got a wrong answer in both directions: the page overstated what
was pending, and the pending list was buried under released work.

The oracle is the release pages themselves rather than ``git log``. This
repository's history is squashed — only 61 commits are reachable from ``v3.15.1``
— so a reachability check would pass by finding nothing, which is the worst way
for a guard to be wrong. ``docs/release-notes/v*.md`` is the durable record of
what a tag contained, and it is checked into the same tree as the claim it
refutes.

What this cannot catch: an entry whose issue or PR number never made it onto its
release page at all. That is a gap in the release page, not in this page, and it
is tracked separately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOTES_DIR = _REPO_ROOT / "docs" / "release-notes"
_UNRELEASED = _NOTES_DIR / "unreleased.md"

_REF = re.compile(r"#(\d{3,5})")

# Entries that name a released issue as *context* rather than as their own
# attribution. Keyed by a distinctive phrase from the entry; the value is why.
#
# An exemption records a decision, so each one is written by hand and says what
# the citation is doing. Remove the row when the entry ships.
CONTEXT_CITATIONS: dict[str, str] = {
    "The eval-gate verdict receipt store no longer follows a symlinked": (
        "cites #3080 as the same containment shape and #3409/#3410 as the subsystem it "
        "guards; the symlink fix itself is unreleased"
    ),
}


def _refs(text: str) -> set[int]:
    return {int(match) for match in _REF.findall(text)}


def _released_refs() -> dict[int, list[str]]:
    """Every issue or PR number named on a tagged release page, and where."""
    seen: dict[int, list[str]] = {}
    for page in sorted(_NOTES_DIR.glob("v*.md")):
        for ref in _refs(page.read_text(encoding="utf-8")):
            seen.setdefault(ref, []).append(page.name)
    return seen


def _bullets(text: str) -> list[tuple[str, str]]:
    """Top-level bullets as ``(first line, whole entry)`` pairs.

    An entry runs from its ``- `` line to the next one or the next heading, so
    a multi-paragraph entry is judged as the single unit a reader sees.
    """
    entries: list[tuple[str, str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("- "):
            if current is not None:
                entries.append((current[0], "\n".join(current)))
            current = [line]
        elif line.startswith("#"):
            if current is not None:
                entries.append((current[0], "\n".join(current)))
            current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        entries.append((current[0], "\n".join(current)))
    return entries


def test_the_unreleased_page_exists() -> None:
    """A missing page must fail loudly rather than degrade to "nothing is stale"."""
    assert _UNRELEASED.is_file(), f"{_UNRELEASED} is missing"


def test_there_are_tagged_release_pages_to_compare_against() -> None:
    """The oracle must have content, or every check below passes vacuously."""
    released = _released_refs()
    assert len(released) > 100, f"only {len(released)} refs found across release pages; the oracle is empty"


def test_no_unreleased_entry_names_work_that_already_shipped() -> None:
    """The rotation guard.

    An entry whose issue or PR number is already written on a tagged release
    page went out under that tag. Leaving it here republishes it, and it is the
    exact failure that let the page grow for three months without anyone
    noticing.
    """
    released = _released_refs()
    stale: list[str] = []
    for first_line, entry in _bullets(_UNRELEASED.read_text(encoding="utf-8")):
        if any(phrase in entry for phrase in CONTEXT_CITATIONS):
            continue
        shipped = sorted(ref for ref in _refs(entry) if ref in released)
        if shipped:
            pages = sorted({page for ref in shipped for page in released[ref]})
            refs = ", ".join(f"#{ref}" for ref in shipped)
            stale.append(f"  {first_line[2:90]}\n      {refs} already documented in {', '.join(pages)}")
    assert not stale, (
        "docs/release-notes/unreleased.md names work that a tagged release already documented.\n"
        "Move each entry to the release page that shipped it if that page is missing it, then delete it here:\n"
        + "\n".join(stale)
    )


def test_the_page_states_the_rule_it_is_held_to() -> None:
    """A reader who trips this guard should find the rule on the page itself."""
    text = _UNRELEASED.read_text(encoding="utf-8")
    assert "since the newest" in text


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("- a (#1234)\n  more text\n- b (#5678)\n", 2),
        ("## Added\n- a (#1234)\n\n## Fixed\n- b (#5678)\n", 2),
        ("no bullets here\n", 0),
        ("- only one (#1234)\n", 1),
    ],
)
def test_the_entry_splitter_sees_what_a_reader_sees(markdown: str, expected: int) -> None:
    """Everything above hangs off this; a splitter that finds nothing passes everything."""
    assert len(_bullets(markdown)) == expected


def test_every_context_exemption_still_matches_an_entry() -> None:
    """An exemption for an entry that shipped is a hole nobody opened on purpose.

    Without this, the row outlives the entry it excused and silently exempts
    whatever text happens to match it next.
    """
    text = _UNRELEASED.read_text(encoding="utf-8")
    orphaned = [phrase for phrase in CONTEXT_CITATIONS if phrase not in text]
    assert not orphaned, f"CONTEXT_CITATIONS rows match no entry any more; delete them: {orphaned}"


def test_a_stale_entry_is_actually_detected() -> None:
    """The guard's own failing case, so a refactor cannot quietly disarm it."""
    released = _released_refs()
    known_shipped = next(iter(sorted(released)))
    entry = f"- something that already went out (#{known_shipped})\n"
    stale = [b for _, b in _bullets(entry) if _refs(b) & released.keys()]
    assert stale, "the guard would not notice an entry naming an already-released ref"
