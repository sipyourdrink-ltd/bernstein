"""Regression coverage keeping SECURITY.md's Supported Versions table in
sync with the project's actual version (#6065).

The table used to be hand-maintained prose, so it silently fell behind the
version in ``pyproject.toml`` release after release. These tests derive the
expected minor line from ``pyproject.toml`` itself rather than hardcoding a
version, so a release bump that forgets to touch ``SECURITY.md`` fails here
instead of shipping a stale policy.

Policy: SECURITY.md states fixes ship on the current release line and there
is no backport branch, so exactly one minor line - the current one - is
supported.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

#: This test reads two fixed repo-root files by path rather than importing
#: any `bernstein` module, so no diff ever produces an import edge to it. A
#: release bump that rewrites `pyproject.toml`'s version and leaves
#: `SECURITY.md` stale is exactly the change this file exists to catch, so it
#: has to run on that bump PR itself, not only on a future PR that happens to
#: touch one of these two files directly (`tests/unit/test_docs_dependency_group.py`
#: is the same shape, and carries the same marker).
pytestmark = pytest.mark.whole_tree_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SECURITY_MD = REPO_ROOT / "SECURITY.md"

#: Skips the header row (`version.lower() == "version"`) and both plain
#: (`---------`) and GFM alignment (`:---:`) separator rows -- a formatter is
#: free to rewrite the separator into the colon form without changing what
#: the table means, and that must not be parsed as a data row.
_ROW_RE = re.compile(r"^\|\s*(?P<version>[^|]+?)\s*\|\s*(?P<supported>[^|]+?)\s*\|\s*$", re.MULTILINE)
_SEPARATOR_CHARS = frozenset("-:")


def _project_minor_version() -> str:
    """Return ``"<major>.<minor>"`` from the ``version`` declared in pyproject.toml."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    version = data["project"]["version"]
    major, minor, *_rest = version.split(".")
    return f"{major}.{minor}"


def _supported_versions_table() -> list[tuple[str, str]]:
    """Parse the ``## Supported Versions`` table in SECURITY.md into (version, supported) rows.

    Skips the header row and the ``---`` separator row.
    """
    text = SECURITY_MD.read_text(encoding="utf-8")
    heading = "## Supported Versions"
    start = text.index(heading)
    section = text[start + len(heading) :]
    next_heading = re.search(r"^## ", section, re.MULTILINE)
    if next_heading is not None:
        section = section[: next_heading.start()]

    rows: list[tuple[str, str]] = []
    for match in _ROW_RE.finditer(section):
        version, supported = match.group("version"), match.group("supported")
        if version.lower() == "version" or set(version) <= _SEPARATOR_CHARS:
            continue
        rows.append((version, supported))
    return rows


def test_pyproject_version_is_parseable() -> None:
    """Sanity check for the fixture the other tests depend on."""
    minor = _project_minor_version()
    assert re.fullmatch(r"\d+\.\d+", minor)


def test_supported_versions_table_has_current_minor_marked_yes() -> None:
    minor = _project_minor_version()
    rows = _supported_versions_table()
    expected_row = (f"{minor}.x", "Yes")
    assert expected_row in rows, (
        f"SECURITY.md's Supported Versions table does not mark the current "
        f"release line ({minor}.x) as supported; got {rows}. "
        f"pyproject.toml declares version {minor}.x -- update the table."
    )


def test_supported_versions_table_marks_everything_else_unsupported() -> None:
    """Current-release-only policy: exactly one row is Yes, and it is the
    current minor. Every other row must be No.

    This is what makes the test catch a *stale* Yes line, not just a missing
    one -- a table that still says the previous minor is supported alongside
    the new one would pass a "current minor is present" check but violates
    the project's stated no-backport-branch policy.
    """
    minor = _project_minor_version()
    rows = _supported_versions_table()
    assert rows, "SECURITY.md's Supported Versions table has no rows to check."

    yes_rows = [version for version, supported in rows if supported == "Yes"]
    assert yes_rows == [f"{minor}.x"], (
        f"Exactly one release line should be marked Yes (the current one, "
        f"{minor}.x) per the no-backport-branch policy; got {yes_rows}."
    )

    for version, supported in rows:
        if version != f"{minor}.x":
            assert supported == "No", f"{version} should be marked No (no backport branch), got {supported!r}."


def test_supported_versions_table_has_no_contradictory_current_row() -> None:
    """The current release line must appear exactly once, marked ``Yes``.

    ``test_supported_versions_table_marks_everything_else_unsupported`` skips
    every row whose version equals the current line before checking
    ``supported == "No"``, so a table listing both ``3.20.x | Yes`` and a
    stray ``3.20.x | No`` passes every other test here: the Yes-row check
    finds its row, and the everything-else check never looks at the
    contradictory one. Require the current line to appear exactly once.
    """
    minor = _project_minor_version()
    rows = _supported_versions_table()
    current_rows = [row for row in rows if row[0] == f"{minor}.x"]
    assert current_rows == [(f"{minor}.x", "Yes")], (
        f"SECURITY.md's Supported Versions table must list the current "
        f"release line ({minor}.x) exactly once, marked Yes; got {current_rows}."
    )


def test_supported_versions_table_names_the_current_unsupported_bound() -> None:
    """The ``< X.Y`` row must name the *current* minor, not a stale one.

    ``test_supported_versions_table_marks_everything_else_unsupported`` only
    requires every non-current row to read ``No`` -- a table with no
    unsupported row at all, or one whose bound still names an old minor
    (``< 3.19`` after the project moved to 3.21), passes that check because it
    never compares the row's *version* against the current line. #6065's own
    acceptance criteria name both the ``Yes`` row and the ``< X.Y`` row, so
    pin the second one explicitly too.
    """
    minor = _project_minor_version()
    rows = _supported_versions_table()
    expected_row = (f"< {minor}", "No")
    assert expected_row in rows, (
        f"SECURITY.md's Supported Versions table must mark every release "
        f"before the current line unsupported with a '< {minor}' row; got "
        f"{rows}."
    )


def test_no_backport_branch_policy_still_documented() -> None:
    """Pins the sentence the version-only-support policy is derived from, so
    a future edit that softens or removes it is a visible diff, not a silent
    policy change."""
    text = SECURITY_MD.read_text(encoding="utf-8")
    assert "Fixes ship on the current release line. There is no backport branch." in text
