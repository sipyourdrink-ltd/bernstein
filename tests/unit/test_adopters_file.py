"""Keep ``ADOPTERS.md`` from decaying into a marketing page.

The list is self-reported and added to by pull request, so the only thing
standing between it and the usual failure modes is review. Two fields decay
first and are the ones worth pinning:

* **Contact.** An entry nobody will stand behind is not evidence that anyone
  uses this. The page says a handle is required; this asserts it.
* **Use case.** "Uses Bernstein" is not a row. A row that could have been
  guessed from the README tells an evaluator nothing, which is the whole
  reason the page exists.

The tests below check the shape of the tables rather than their contents, so
an empty list passes -- an empty ``ADOPTERS.md`` with clear instructions is a
correct starting state, and a padded one is the failure this page invites.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADOPTERS = REPO_ROOT / "ADOPTERS.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

#: The three tables the page is required to carry, by heading.
REQUIRED_SECTIONS = ("Production", "Evaluation / Pilot", "Former adopters")

#: Placeholder rows that stand in for an empty table. These are the one kind of
#: row allowed to skip the field checks below.
_EMPTY_ROW = re.compile(r"^\|\s*_No entries yet\._\s*\|")

#: Phrases too generic to be a use case. Anything that merely restates the
#: project's own name is not telling a reader something new.
_VAGUE_USE_CASES = frozenset(
    {"uses bernstein", "bernstein", "governance", "n/a", "tbd", "-", "various"}
)


def _adopters_text() -> str:
    return ADOPTERS.read_text(encoding="utf-8")


def _rows(section: str) -> list[list[str]]:
    """Return the data rows of one section's table, as stripped cell lists.

    Header and separator rows are dropped; so are the ``_No entries yet._``
    placeholders, which exist so an empty table still renders.
    """
    text = _adopters_text()
    start = text.index(f"\n## {section}\n")
    rest = text[start + 1 :]
    end = rest.find("\n## ")
    body = rest if end == -1 else rest[:end]

    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or _EMPTY_ROW.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Skip the header row and the `| --- |` separator beneath it.
        if cells[0].lower().startswith("organization") or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append(cells)
    return rows


def test_the_file_exists() -> None:
    assert ADOPTERS.is_file(), "ADOPTERS.md must live at the repository root"


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_every_required_section_is_present(section: str) -> None:
    """A pilot and a production dependency are different claims.

    They are kept in separate tables so the difference does not need a squint,
    and ``Former adopters`` exists so a lapsed entry is demoted rather than
    quietly deleted.
    """
    assert f"\n## {section}\n" in _adopters_text()


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_every_table_declares_the_full_column_set(section: str) -> None:
    text = _adopters_text()
    start = text.index(f"\n## {section}\n")
    body = text[start:]
    header = next(line for line in body.splitlines() if line.strip().startswith("| Organization"))
    columns = [c.strip().lower() for c in header.strip().strip("|").split("|")]

    assert columns[0].startswith("organization")
    assert columns[1] == "domain"
    assert columns[2].startswith("what they use")
    # "Since" for current adopters, "Last confirmed" for former ones.
    assert columns[3] in {"since", "last confirmed"}
    assert columns[4] == "contact"


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_every_row_names_a_contact(section: str) -> None:
    """A row nobody will stand behind is not evidence."""
    for row in _rows(section):
        assert row[4], f"{section}: {row[0]!r} has no contact handle"


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_every_row_states_a_specific_use_case(section: str) -> None:
    for row in _rows(section):
        use_case = row[2]
        assert use_case, f"{section}: {row[0]!r} has no use case"
        assert use_case.lower().rstrip(".") not in _VAGUE_USE_CASES, (
            f"{section}: {row[0]!r} has a use case that restates the project "
            f"rather than describing a deployment: {use_case!r}"
        )


def test_the_page_states_the_rule_that_keeps_it_honest() -> None:
    """The rules are the artefact. Losing them turns this into a logo wall."""
    text = _adopters_text().lower()
    assert "self-reported" in text
    assert "inference is not consent" in text
    assert "contact handle is required" in text


def test_the_pruning_rule_names_a_period() -> None:
    """"Stale entries get pruned" is not a rule until it says when."""
    section = _adopters_text().split("## Production")[0]
    assert re.search(r"\b\d+\s+days\b", section), (
        "the pruning rule must name a concrete period, not just promise pruning"
    )


def test_contributing_tells_people_how_to_add_themselves() -> None:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "ADOPTERS.md" in text, "CONTRIBUTING.md must link to the adopters list"
