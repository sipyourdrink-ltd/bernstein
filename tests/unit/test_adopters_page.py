"""`ADOPTERS.md` rows carry the two fields that decay first.

The page is self-reported, so its integrity cannot come from review alone: a
row is added by the adopter, in their own pull request, and nobody else is in a
position to know whether the use case is still true. What *can* be enforced
mechanically is that a row was written to be checkable in the first place --
that somebody is named who will stand behind it, and that the use case says
something more than "uses Bernstein" (#5027).

An empty table passes every assertion here, deliberately. An empty
`ADOPTERS.md` is a correct state; a padded one is the failure mode.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE = REPO_ROOT / "ADOPTERS.md"

#: The column set both tables carry.
COLUMNS = ("Organization or project", "Domain", "What they use it for", "Since", "Contact")

#: Section headings that must exist. A pilot and a production dependency are
#: different claims; collapsing them into a status column would let one read as
#: the other at a glance.
SECTIONS = ("## Production", "## Evaluation / Pilot")

#: Placeholder use cases -- the shape the "specific use case" rule exists to
#: refuse. Matched case-insensitively against the whole cell.
_VAGUE = frozenset({"uses bernstein", "uses it", "various", "tbd", "n/a", "-", ""})


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


def _data_rows() -> list[list[str]]:
    """Every table row that is not a header or separator."""
    rows: list[list[str]] = []
    for line in _text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells[0] == COLUMNS[0]:
            continue
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        rows.append(cells)
    return rows


def test_the_page_exists_with_both_sections() -> None:
    """Two sections, not one table with a status column."""
    assert PAGE.is_file()
    text = _text()
    for section in SECTIONS:
        assert section in text, f"ADOPTERS.md is missing the `{section}` section"


def test_both_tables_carry_the_agreed_columns() -> None:
    """A row is only comparable to another row if the columns are the same."""
    headers = [line for line in _text().splitlines() if line.strip().startswith(f"| {COLUMNS[0]}")]
    assert len(headers) == 2, "expected one header row per section"
    for header in headers:
        cells = [cell.strip() for cell in header.strip().strip("|").split("|")]
        assert tuple(cells) == COLUMNS


def test_the_contribution_rule_is_stated_on_the_page() -> None:
    """The rule is enforced socially, so it has to be legible where rows are added."""
    text = _text().lower()
    assert "inference is not consent" in text
    assert "self-reported" in text


def test_the_pruning_rule_is_stated_on_the_page() -> None:
    """A list with no removal rule becomes a list of things that used to be true."""
    assert "## Pruning" in _text()


@pytest.mark.parametrize("row", _data_rows() or [None])
def test_every_row_names_a_contact_and_a_specific_use_case(row: list[str] | None) -> None:
    """The two fields that decay first, and the two nobody can reconstruct later.

    An entry nobody will stand behind does not go in, and a use case that says
    nothing makes the page unreadable to the next evaluator.
    """
    if row is None:
        pytest.skip("no adopters listed yet — an empty page is a correct state")
    assert len(row) == len(COLUMNS), f"row has {len(row)} cells, expected {len(COLUMNS)}: {row}"
    organization, _domain, use_case, since, contact = row
    assert organization, "every row needs an organization or project name"
    assert contact, f"{organization}: a row needs a contact handle nobody has to guess at"
    assert use_case.lower() not in _VAGUE, (
        f"{organization}: '{use_case}' is not a use case. "
        "Name what it does for you, e.g. 'deterministic replay for regulated "
        "evidence in an air-gapped environment'."
    )
    assert re.fullmatch(r"\d{4}-\d{2}", since), (
        f"{organization}: `Since` should be a month, like 2026-03, not {since!r}"
    )
