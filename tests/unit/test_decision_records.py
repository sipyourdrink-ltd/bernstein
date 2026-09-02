"""Guard: the decision record under ``docs/decisions/`` stays readable.

The directory is prose, so nothing fails when it drifts: a record can lose
its ``Status``, a number can go missing, a superseded record can point at a
successor that was never written, and the index can list a file that no
longer exists. Each of those is invisible until a contributor reads the
directory and cannot tell which record is binding.

These checks pin the structure the directory already relies on, the same way
``test_docs_url_hygiene`` pins the docs host in shipped source.
"""

from __future__ import annotations

import re
from pathlib import Path

_DECISIONS = Path(__file__).resolve().parents[2] / "docs" / "decisions"
_INDEX = _DECISIONS / "index.md"

_RECORD_NAME = re.compile(r"^(\d{3})-[a-z0-9-]+\.md$")
_STATUS = re.compile(r"^\*\*Status\*\*:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_DATE = re.compile(r"^\*\*Date\*\*:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUPERSEDED_BY = re.compile(r"^Superseded by ADR-(\d{3})\b")
_INDEX_LINK = re.compile(r"\]\((\d{3}-[a-z0-9-]+\.md)\)")
_ISSUE_CITATION = re.compile(r"(?<![\w/])#(\d+)\b")
_GITHUB_URL = re.compile(r"https://github\.com/([^/\s)\]`]+/[^/\s)\]`]+)")

_REPOSITORY = "sipyourdrink-ltd/bernstein"


def _records() -> dict[int, Path]:
    """Every numbered decision record, keyed by its number."""
    found: dict[int, Path] = {}
    for path in sorted(_DECISIONS.glob("*.md")):
        match = _RECORD_NAME.match(path.name)
        if match is None:
            continue
        found[int(match.group(1))] = path
    return found


def _header_field(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    return None if match is None else match.group("value").strip()


def test_every_decision_record_declares_status_and_date() -> None:
    """A record without a status or a date cannot be acted on.

    The reader's first question is "does this still hold, and since when".
    """
    missing: list[str] = []
    for number, path in sorted(_records().items()):
        text = path.read_text(encoding="utf-8")
        status = _header_field(text, _STATUS)
        date = _header_field(text, _DATE)
        if not status:
            missing.append(f"{path.name}: no '**Status**:' line")
        if not date:
            missing.append(f"{path.name}: no '**Date**:' line")
        elif not _ISO_DATE.match(date):
            missing.append(f"{path.name}: '**Date**: {date}' is not YYYY-MM-DD")
        del number
    assert missing == [], "decision records must carry a status and an ISO date:\n" + "\n".join(
        missing
    )


def test_decision_record_numbering_has_no_unexplained_gap() -> None:
    """Numbers run 001..N with nothing missing.

    A number nobody can account for is a question every future reader has to
    ask once. A record that was withdrawn keeps its number and says so.
    """
    numbers = sorted(_records())
    assert numbers, f"no decision records found under {_DECISIONS}"
    expected = list(range(1, numbers[-1] + 1))
    gaps = sorted(set(expected) - set(numbers))
    assert gaps == [], (
        "decision-record numbering has gaps at "
        + ", ".join(f"{n:03d}" for n in gaps)
        + "; a number that is no longer in use keeps a record stating that, "
        "and is never reused"
    )


def test_a_superseded_record_names_a_successor_that_exists() -> None:
    """``Superseded`` is only useful when it says by what.

    A record marked superseded with no successor is worse than one left
    accepted: the reader knows it is stale and still has nowhere to go.
    """
    records = _records()
    broken: list[str] = []
    for number, path in sorted(records.items()):
        status = _header_field(path.read_text(encoding="utf-8"), _STATUS) or ""
        if not status.lower().startswith("superseded"):
            continue
        match = _SUPERSEDED_BY.match(status)
        if match is None:
            broken.append(f"{path.name}: '**Status**: {status}' does not name a successor")
            continue
        successor = int(match.group(1))
        if successor not in records:
            broken.append(f"{path.name}: names ADR-{successor:03d}, which does not exist")
        elif successor == number:
            broken.append(f"{path.name}: names itself as its successor")
    assert broken == [], (
        "a superseded record must read 'Superseded by ADR-NNN' and point at a "
        "record that exists:\n" + "\n".join(broken)
    )


def test_index_row_exists_for_every_record_and_every_row_resolves() -> None:
    """The index is the entry point, so it must match the directory.

    A record the index omits is unfindable; a row pointing at a deleted file
    is a dead link in the one place a reader starts.
    """
    index_text = _INDEX.read_text(encoding="utf-8")
    linked = set(_INDEX_LINK.findall(index_text))
    on_disk = {path.name for path in _records().values()}

    unlisted = sorted(on_disk - linked)
    dangling = sorted(linked - on_disk)
    assert unlisted == [], f"{_INDEX.name} does not list: {', '.join(unlisted)}"
    assert dangling == [], f"{_INDEX.name} links files that do not exist: {', '.join(dangling)}"


def test_issue_citations_are_well_formed_and_point_at_this_repository() -> None:
    """An ADR derived from a thread has to be traceable back to it.

    A citation with a leading zero or a number of zero resolves to nothing,
    and a GitHub link to another repository resolves to someone else's
    numbering — both read as evidence and are not.
    """
    bad: list[str] = []
    for path in sorted(_records().values()) + [_INDEX]:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for raw in _ISSUE_CITATION.findall(line):
                if raw != str(int(raw)) or int(raw) == 0:
                    bad.append(f"{path.name}:{lineno}: '#{raw}' is not an issue number")
            for repo in _GITHUB_URL.findall(line):
                if repo != _REPOSITORY:
                    bad.append(f"{path.name}:{lineno}: links {repo}, not {_REPOSITORY}")
    assert bad == [], "issue citations in decision records must resolve:\n" + "\n".join(bad)
