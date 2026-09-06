"""Keep ``docs/integrations.md`` from rotting into a claim nobody checked.

The page maps systems Bernstein bridges to -- Okta, Vault, OpenTelemetry --
onto three columns: shipped, open, not planned. Its whole value is that a
reader can trust a cell without opening the source, so every cell has to be
verifiable from the repository:

* a **shipped** cell names a module path, and the module has to exist at
  ``HEAD``;
* an **open** cell names an issue number, in a link a reader can follow;
* a **not planned** cell carries a reason, because a bare "no" is the thing
  that sends people to the tracker to ask anyway.

A rotted integration index is worse than none, so these are assertions rather
than a style guide.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "integrations.md"
README = REPO_ROOT / "README.md"

#: ``src/bernstein/...`` or ``src/bernstein/...py`` inside backticks. Trailing
#: prose after an em dash is the human description and is not part of the path.
_MODULE_RE = re.compile(r"`(src/bernstein/[A-Za-z0-9_/]+(?:\.py)?)`")

#: A tracker link, which is the only form an "open" cell may take -- a bare
#: ``#5040`` renders as a fragment and is not followable from the docs site.
_ISSUE_LINK_RE = re.compile(
    r"\[#(\d+)\]\(https://github\.com/sipyourdrink-ltd/bernstein/issues/(\d+)\)"
)


def _rows() -> list[tuple[str, list[str]]]:
    """Return ``(section, cells)`` for every data row in every table."""
    section = ""
    rows: list[tuple[str, list[str]]] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:]
            continue
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) != 4:
            continue
        if cells[0] == "Target" or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append((section, cells))
    return rows


def test_the_page_exists() -> None:
    assert DOC.is_file()


def test_the_page_has_rows() -> None:
    """A parser that silently matches nothing would make every test below pass."""
    assert len(_rows()) >= 15, "the index should cover the named targets in #5023"


@pytest.mark.parametrize(
    "kind",
    [
        "Directories and identity providers",
        "Authorization and policy engines",
        "Secret stores",
        "Workload identity",
        "Telemetry and lineage",
        "Agent protocols",
    ],
)
def test_every_required_kind_has_a_section(kind: str) -> None:
    assert f"\n## {kind}\n" in DOC.read_text(encoding="utf-8")


def test_no_row_claims_shipped_without_a_module_path() -> None:
    """The acceptance criterion from #5023, stated directly."""
    for section, cells in _rows():
        shipped = cells[1]
        if not shipped:
            continue
        assert _MODULE_RE.search(shipped), (
            f"{section}: {cells[0]!r} claims shipped but names no module path: {shipped!r}"
        )


def test_every_shipped_module_exists_at_head() -> None:
    checked = 0
    for section, cells in _rows():
        for path in _MODULE_RE.findall(cells[1]):
            target = REPO_ROOT / path
            assert target.exists(), (
                f"{section}: {cells[0]!r} cites {path}, which does not exist at HEAD"
            )
            checked += 1
    assert checked >= 10, "expected the index to cite real modules, not just prose"


def test_every_open_cell_links_an_issue() -> None:
    for section, cells in _rows():
        open_cell = cells[2]
        if not open_cell:
            continue
        matches = _ISSUE_LINK_RE.findall(open_cell)
        assert matches, (
            f"{section}: {cells[0]!r} has an 'open' cell that links no issue: {open_cell!r}"
        )
        for shown, linked in matches:
            assert shown == linked, (
                f"{section}: {cells[0]!r} shows #{shown} but links to issue {linked}"
            )


def test_every_not_planned_cell_gives_a_reason() -> None:
    """A bare 'no' sends the reader to the tracker to ask anyway."""
    for section, cells in _rows():
        reason = cells[3]
        if not reason:
            continue
        assert len(reason.split()) >= 8, (
            f"{section}: {cells[0]!r} declines without saying why: {reason!r}"
        )


def test_no_row_is_entirely_empty() -> None:
    """Shipped, open, or not planned -- a row citing none of the three is deleted."""
    for section, cells in _rows():
        assert any(cells[1:]), f"{section}: {cells[0]!r} cites no evidence in any column"


def test_the_readme_links_the_index() -> None:
    assert "docs/integrations.md" in README.read_text(encoding="utf-8")


def test_the_docs_nav_lists_the_index() -> None:
    assert "integrations.md" in (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
