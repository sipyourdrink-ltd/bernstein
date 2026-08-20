"""The feature matrix must track the CLI surface it claims to index.

``docs/reference/FEATURE_MATRIX.md`` is the exhaustive capability index the
README links to, and it was the only major reference page with nothing
holding it to the code.  ``docs/reference/cli-reference.md`` has
``test_cli_command_registration.py``; ``docs/reference/capabilities.md`` has
``test_docs_capability_reachability.py``.  The matrix had neither, so a
command could ship, be documented everywhere else, and never reach the index
a reader is pointed at for the full picture -- with every check green.

What this file asserts
----------------------

* **Forward.** Every command registered on the top-level ``bernstein`` group
  is named by some matrix row, or is listed in ``UNLISTED_COMMANDS`` below.
  A newly registered command is in neither, so it fails here.
* **Reverse.** No matrix row names a command the CLI does not register.  A
  reader following the matrix must not land on "No such command".  There are
  no exemptions to this direction and none should be added: a row naming a
  deleted verb is a factual error, not staleness.
* **Pin hygiene.** ``UNLISTED_COMMANDS`` may only shrink.  An entry for a
  command that has since gained a row fails, and so does an entry for a
  command that no longer exists -- either way the pin cannot rot into a
  list that exempts nothing while reading as if it did.
* **Column shape.** Every row carries a ``Maturity`` score in 1-5, and the
  legend at the top of the page defines every score the table uses.

Scope, stated precisely: this is a check of *names*, at the top level of the
command tree.  A verb named anywhere in a table row counts as indexed,
including in a Notes cell; prose outside the tables does not count.  Whether
a row's prose is accurate is not decidable here and is not claimed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bernstein.cli.main import cli

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MATRIX = _REPO_ROOT / "docs" / "reference" / "FEATURE_MATRIX.md"

#: ``bernstein <verb>``, plus any slash alternatives written directly after
#: the verb.  ``bernstein review/approve/reject/pending`` names four
#: top-level commands; ``bernstein mandate emit/verify/revoke`` names one,
#: because there the slash run follows a subcommand.
_VERB = re.compile(r"bernstein ([a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*)")

#: Markdown table cell separator: a pipe that is not backslash-escaped.
_CELL_SPLIT = re.compile(r"(?<!\\)\|")

#: Registered commands with no row in the matrix yet.
#:
#: The matrix indexes the operator-facing surface, and the command tree has
#: outgrown it: these are reachable today and unlisted.  The list is a pin,
#: not a permission -- it exists so a *newly* registered command fails the
#: forward check instead of joining a backlog nobody can see.  Adding a row
#: means deleting the entry here; the tests below enforce both halves.
UNLISTED_COMMANDS: frozenset[str] = frozenset(
    {
        "ab-test",
        "abandonments",
        "adapters",
        "agents-md",
        "aliases",
        "analyze",
        "api-check",
        "approve-tool",
        "artifact",
        "artifacts",
        "auth",
        "backlog",
        "bench",
        "best-of-n",
        "blast-radius",
        "bom",
        "bundle",
        "changelog",
        "chat",
        "cleanup",
        "cluster",
        "compare",
        "compliance",
        "config-path",
        "conn",
        "context",
        "cook",
        "cost-envelopes",
        "criterion-profile",
        "ctx",
        "daemon",
        "debug-bundle",
        "decisions",
        "dep-impact",
        "desktop-register",
        "dr",
        "dry-run",
        "estimate",
        "explain",
        "export",
        "fingerprint",
        "from-ticket",
        "git",
        "graph",
        "gui",
        "handoff",
        "help-all",
        "history",
        "hook-gate",
        "hooks",
        "impact",
        "init-wizard",
        "integrations",
        "interop",
        "issue-to-pr",
        "knowledge",
        "limits",
        "list-tasks",
        "login",
        "man-pages",
        "merge",
        "migrate",
        "pipeline",
        "policy",
        "pr",
        "profile",
        "prompts",
        "quality",
        "readme-l10n",
        "recipes",
        "reject-tool",
        "remote",
        "resume",
        "routine",
        "run-changelog",
        "run-lookup",
        "sandbox",
        "scaffold",
        "secrets",
        "security",
        "serve",
        "session",
        "simulate",
        "skill",
        "slo",
        "start",
        "task",
        "tasks",
        "team",
        "templates",
        "test",
        "ticket",
        "trackers",
        "trend-scan",
        "triggers",
        "tunnel",
        "undo",
        "validate",
        "var",
        "wheelhouse",
        "wiki",
        "worktrees",
    }
)

_FIX_HINT = (
    "Add a row to docs/reference/FEATURE_MATRIX.md (the CLI commands section) "
    "with a Docs status and a Maturity score, or -- if the command is not part "
    "of the operator-facing surface -- add it to UNLISTED_COMMANDS in "
    f"{Path(__file__).name} with the reason in the commit message."
)


def _table_rows() -> list[list[str]]:
    """Cells of every content row of every table in the matrix.

    Header rows (``Capability`` / ``Command`` / ``Maturity``) and separator
    rows are dropped; what is left is one list of cells per capability.

    Cells are split on unescaped pipes only: ``bernstein acp serve
    --stdio\\|--http :PORT`` writes a literal pipe inside one cell, and
    splitting on it would report that row as malformed while silently
    truncating its Notes.
    """
    rows: list[list[str]] = []
    for line in _MATRIX.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped) <= {"|", "-", ":", " "}:
            continue
        cells = [cell.strip() for cell in _CELL_SPLIT.split(stripped.strip("|"))]
        if cells[0] in {"Capability", "Command", "Maturity"}:
            continue
        rows.append(cells)
    return rows


def _capability_rows() -> list[list[str]]:
    """Capability rows only -- the four-column tables, not the legend."""
    return [cells for cells in _table_rows() if len(cells) == 4]


def documented_commands() -> set[str]:
    """Top-level ``bernstein`` verbs named by some row of the matrix."""
    found: set[str] = set()
    for cells in _table_rows():
        for match in _VERB.findall(" | ".join(cells)):
            found.update(match.split("/"))
    return found


def registered_commands() -> set[str]:
    return set(cli.commands)


def test_registered_commands_have_a_matrix_row() -> None:
    """A command the CLI registers is indexed by the matrix, or pinned."""
    unindexed = sorted(registered_commands() - documented_commands() - UNLISTED_COMMANDS)
    assert not unindexed, (
        f"registered on the CLI but named nowhere in docs/reference/FEATURE_MATRIX.md: {unindexed}. {_FIX_HINT}"
    )


def test_matrix_rows_name_live_commands() -> None:
    """No row points a reader at a command that does not resolve.

    Deliberately without an exemption list: a documented verb the CLI does
    not register fails with "No such command" in the reader's terminal, which
    is a defect rather than a lag.
    """
    ghosts = sorted(documented_commands() - registered_commands())
    assert not ghosts, (
        f"named in docs/reference/FEATURE_MATRIX.md but not registered on the CLI: {ghosts}. "
        "Remove or correct the row -- following it produces 'No such command'."
    )


def test_pinned_commands_are_still_registered() -> None:
    """Every pinned command still exists, so the pin cannot rot."""
    stale = sorted(UNLISTED_COMMANDS - registered_commands())
    assert not stale, (
        f"UNLISTED_COMMANDS names commands the CLI no longer registers: {stale}. "
        f"Delete them from the set in {Path(__file__).name}."
    )


def test_pinned_commands_have_no_row() -> None:
    """A pinned command that has since gained a row leaves the pin.

    Without this the set would keep exempting commands the matrix already
    covers, and the forward check would quietly stop applying to them.
    """
    covered = sorted(UNLISTED_COMMANDS & documented_commands())
    assert not covered, (
        f"UNLISTED_COMMANDS names commands the matrix now indexes: {covered}. "
        f"Delete them from the set in {Path(__file__).name} so the forward check covers them again."
    )


def test_every_row_carries_a_maturity_score() -> None:
    """Every capability row scores 1-5 in the Maturity column."""
    bad = [(cells[0], cells[2]) for cells in _capability_rows() if cells[2] not in {"1", "2", "3", "4", "5"}]
    assert not bad, f"rows whose Maturity cell is not a score in 1-5: {bad}"


def test_every_table_has_four_columns() -> None:
    """A row that lost a cell would silently shift the Maturity column."""
    malformed = [cells for cells in _table_rows() if len(cells) not in {2, 4}]
    assert not malformed, f"rows with neither 2 (legend) nor 4 (capability) cells: {malformed}"


def test_legend_defines_every_score_in_use() -> None:
    """The legend explains each score the table actually uses."""
    text = _MATRIX.read_text(encoding="utf-8")
    used = {cells[2] for cells in _capability_rows()}
    undefined = sorted(score for score in used if not re.search(rf"^\| {score} \| \S", text, re.MULTILINE))
    assert not undefined, f"Maturity scores used by rows but absent from the legend: {undefined}"


@pytest.mark.parametrize("command", ["audit", "lineage", "report", "listen"])
def test_named_verification_surfaces_stay_indexed(command: str) -> None:
    """Spot-check the verbs the index is read for.

    Kept separate from the sweep so a regression names the command instead of
    only reporting that some command fell out of the matrix.
    """
    assert command in documented_commands(), f"`bernstein {command}` is not named by any matrix row"


def _matrix_row_for(command: str) -> str:
    """Return the feature-matrix row that documents a command."""
    for line in _MATRIX.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("|") and command in line:
            return line
    raise AssertionError(f"No feature-matrix row found for {command!r}")


@pytest.mark.parametrize(
    ("command", "entry_page", "entry_marker", "blocker"),
    [
        (
            "bernstein evolve",
            _REPO_ROOT / "docs" / "reference" / "cli-reference.md",
            "> **Preview:** `bernstein evolve run`",
            ".sdd/",
        ),
        (
            "bernstein listen",
            _REPO_ROOT / "docs" / "operations" / "voice-control.md",
            "> **Preview:** `bernstein listen`",
            "bernstein[voice]",
        ),
        (
            "`bernstein demo` |",
            _REPO_ROOT / "docs" / "getting-started" / "first-run.md",
            "UnicodeEncodeError",
            "UnicodeEncodeError",
        ),
        (
            "bernstein demo --flask-todo",
            _REPO_ROOT / "docs" / "getting-started" / "quickstart-demo.md",
            "all three seeded tasks fail",
            "all three seeded tasks fail",
        ),
    ],
)
def test_preview_fences_stay_visible(
    command: str,
    entry_page: Path,
    entry_marker: str,
    blocker: str,
) -> None:
    """Fenced maturity-2 commands keep their Preview marker and blocker."""
    row = _matrix_row_for(command)

    assert "| 2 |" in row
    assert "**Preview.**" in row
    assert blocker in row

    entry_text = entry_page.read_text(encoding="utf-8")

    assert entry_marker in entry_text
    assert blocker in entry_text


def test_cloudflare_section_fenced_as_preview() -> None:
    """Cloud / Cloudflare rows at maturity <= 2 must carry the Preview marker."""
    text = _MATRIX.read_text(encoding="utf-8")

    start = text.find("## Cloud / Cloudflare")
    assert start != -1, "Cloud / Cloudflare section not found in feature matrix"

    next_section_start = text.find("\n---\n", start)
    if next_section_start == -1:
        section_text = text[start:]
    else:
        section_text = text[start:next_section_start]

    checked = 0
    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= {"|", "-", ":", " "}:
            continue
        cells = [cell.strip() for cell in _CELL_SPLIT.split(stripped.strip("|"))]
        if len(cells) != 4 or cells[0] in {"Capability", "Command", "Maturity"}:
            continue

        capability, _docs, maturity, notes = cells
        if maturity.isdigit() and int(maturity) <= 2:
            assert "**Preview.**" in notes, (
                f"Cloud / Cloudflare row '{capability}' has maturity {maturity} "
                "but is missing the **Preview.** marker in its Notes."
            )
            checked += 1

    # Prevent the test from passing silently if parsing breaks
    assert checked >= 9, f"expected to check the Cloud / Cloudflare rows, checked {checked}"

    # Assert the entry page carries the warning
    entry_page = _REPO_ROOT / "docs" / "cloudflare" / "cloudflare-overview.md"
    entry_text = entry_page.read_text(encoding="utf-8")
    assert "> **Preview:**" in entry_text, "cloudflare-overview.md missing the Preview entry marker"
