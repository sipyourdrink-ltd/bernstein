"""Regression coverage keeping SECURITY.md's Supported Versions table in
sync with the project's actual version (#6065).

The table used to be hand-maintained prose, so it silently fell behind the
version in ``pyproject.toml`` release after release. These tests derive the
expected minor line from ``pyproject.toml`` itself rather than hardcoding a
version, so a release bump that forgets to touch ``SECURITY.md`` fails here
instead of shipping a stale policy.

Exactly one minor line receives regular fixes. During 4.0.x, the table
also identifies the single planned backport to the previous minor.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

#: This test reads fixed repository files by path rather than importing
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


def test_project_minor_version_ignores_a_prerelease_patch_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-release version like ``3.21.0a1`` must still resolve to ``3.21``.

    ``_project_minor_version`` only splits on ``.`` and keeps the first two
    components; it never validates or parses the third, so a PEP 440
    pre-release suffix on the patch component (``0a1``, ``0rc1``, ...) cannot
    make the function raise or return the wrong minor line.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "3.21.0a1"\n', encoding="utf-8")
    monkeypatch.setattr("tests.unit.test_security_md.PYPROJECT", pyproject)

    assert _project_minor_version() == "3.21"


def test_supported_versions_table_has_one_current_line_marked_yes() -> None:
    current_line = f"{_project_minor_version()}.x"
    current_rows = [row for row in _supported_versions_table() if row[0] == current_line]
    assert current_rows == [(current_line, "Yes")], (
        f"SECURITY.md must list the current release line ({current_line}) exactly once, marked Yes; got {current_rows}."
    )


def test_supported_versions_table_names_only_the_planned_exception() -> None:
    minor = _project_minor_version()
    other_lines = [row for row in _supported_versions_table() if row[0] not in {f"{minor}.x", "All other versions"}]
    expected = [("3.24.x", "3.24.2 backport only")] if minor == "4.0" else []
    assert other_lines == expected


def test_supported_versions_table_has_one_unsupported_catchall() -> None:
    catchall = "All other versions"
    catchall_rows = [row for row in _supported_versions_table() if row[0] == catchall]
    assert catchall_rows == [(catchall, "No")]


def test_supported_versions_table_orders_current_line_exception_then_catchall() -> None:
    minor = _project_minor_version()
    expected = [f"{minor}.x"]
    if minor == "4.0":
        expected.append("3.24.x")
    expected.append("All other versions")
    assert [version for version, _supported in _supported_versions_table()] == expected


def test_backport_policy_matches_the_release_notes() -> None:
    if _project_minor_version() == "4.0":
        policy = "Regular fixes ship on 4.0.x. The only planned 3.24.x backport is 3.24.2."
        assert policy in SECURITY_MD.read_text(encoding="utf-8").splitlines()
        cut = REPO_ROOT / "scripts" / "release_cuts" / "v4.0.0.toml"
        assert policy in tomllib.loads(cut.read_text(encoding="utf-8"))["limitations"]
        page = REPO_ROOT / "docs" / "release-notes" / "v4.0.0.md"
        limitations = page.read_text(encoding="utf-8").split("## Known limitations\n", 1)[1]
        assert f"- {policy}" in limitations.splitlines()
