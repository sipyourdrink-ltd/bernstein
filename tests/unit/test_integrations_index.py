"""`docs/integrations.md` cites only paths that exist.

The page exists so an evaluator can search for the name of the thing they run
-- Okta, SCIM, Vault, OpenTelemetry -- and find out whether Bernstein talks to
it (#5023). That is only useful while every claim on it is checkable, and a
docs page full of module paths is exactly the artefact that rots first: a
module moves, the row keeps naming the old path, and the page quietly becomes
a list of things that used to be true.

So every inline-code path on the page is resolved here. A row that cannot cite
a real file is deleted, not softened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE = REPO_ROOT / "docs" / "integrations.md"

#: Inline code that looks like a repo path: `core/security/sso_oidc.py`,
#: `tests/unit/test_sso_oidc.py`, `src/bernstein/mcp/`, `core/protocols/a2a/`.
_PATH_LIKE = re.compile(r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]*)`")

#: Paths on the page are written relative to `src/bernstein` unless they
#: already start at a repo root directory, which keeps the table narrow enough
#: to read.
_ROOT_PREFIXES = ("src/", "tests/", "docs/", "scripts/", ".github/")


def _cited_paths() -> list[str]:
    return sorted({match.group(1) for match in _PATH_LIKE.finditer(PAGE.read_text(encoding="utf-8"))})


def _resolve(cited: str) -> Path:
    """Where a cited path should be found on disk."""
    if cited.startswith(_ROOT_PREFIXES):
        return REPO_ROOT / cited
    return REPO_ROOT / "src" / "bernstein" / cited


def test_the_page_exists_and_cites_something() -> None:
    """A page with no citations would pass every other test here vacuously."""
    assert PAGE.is_file()
    assert len(_cited_paths()) > 15, "integrations.md should cite the modules it claims"


@pytest.mark.parametrize("cited", _cited_paths())
def test_every_cited_path_exists(cited: str) -> None:
    """The rot this page is most exposed to: a module that moved."""
    resolved = _resolve(cited)
    assert resolved.exists(), (
        f"docs/integrations.md cites `{cited}`, which resolves to {resolved} and does not exist. "
        "Update the row or delete it — a row that cannot cite a real file is not a weaker claim, "
        "it is not a claim."
    )


def test_every_issue_link_points_at_this_repository() -> None:
    """An "open" cell has to be an issue a reader can actually open."""
    text = PAGE.read_text(encoding="utf-8")
    links = re.findall(r"\]\((https://github\.com/[^)]+)\)", text)
    assert links, "the page should cite issue numbers as links"
    assert all(link.startswith("https://github.com/sipyourdrink-ltd/bernstein/issues/") for link in links)


def test_the_wired_column_is_present_on_every_table() -> None:
    """Shipped and wired are different questions, and the page promises both.

    Dropping the column would turn a caller-less module back into a plain
    "shipped" row, which is the misreading the page exists to prevent.
    """
    headers = [line for line in PAGE.read_text(encoding="utf-8").splitlines() if line.startswith("| Target")]
    assert headers, "expected at least one integration table"
    assert all("Wired" in header for header in headers)
