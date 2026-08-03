"""Guard: the naming policy stays present and stays reachable.

Apache-2.0 grants every right to the code but explicitly withholds the
project name (section 6). The repository answered the code half of that
and left the name half undocumented, so a fork had to guess what it could
call itself and a user filing a bug had no way to check whose build they
were running.

`NOTICE` and `TRADEMARKS.md` close that gap, but a policy nobody can find
is the same as no policy: the failure mode is not the file being edited,
it is the file being orphaned when `README.md` or `CONTRIBUTING.md` is
next restructured, and nothing failing until someone goes looking. These
tests pin the documents and both entry points that lead to them, so
removing either the policy or a pointer to it fails CI instead of
surfacing months later.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTICE = REPO_ROOT / "NOTICE"
TRADEMARKS = REPO_ROOT / "TRADEMARKS.md"
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _headings(text: str) -> list[str]:
    return [line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("## ")]


def test_notice_names_project_and_license() -> None:
    """A NOTICE that does not say whose work it covers is decoration."""
    assert NOTICE.is_file(), "Apache-2.0 projects carry a NOTICE file at the repository root"
    text = NOTICE.read_text(encoding="utf-8")
    for token in ("Bernstein", "Copyright", "LICENSE"):
        assert token in text, (
            f"NOTICE must state the project, the copyright holder, and point at LICENSE; missing {token!r}"
        )


def test_trademarks_policy_covers_grant_ask_and_contact() -> None:
    """The policy is only useful if it answers all three questions a fork actually has."""
    assert TRADEMARKS.is_file(), "the naming policy lives in TRADEMARKS.md at the repository root"
    text = TRADEMARKS.read_text(encoding="utf-8")
    headings = _headings(text)

    assert any("without asking" in heading for heading in headings), (
        f"TRADEMARKS.md must say what derivative works may do without permission; headings are {headings}"
    )
    assert any("ask first" in heading for heading in headings), (
        f"TRADEMARKS.md must say what needs permission first; headings are {headings}"
    )
    assert EMAIL.search(text), "TRADEMARKS.md must give a contact for permission requests"


def test_readme_points_readers_at_naming_policy() -> None:
    """The README is where a user checks whether their build is the official one."""
    assert "TRADEMARKS.md" in README.read_text(encoding="utf-8"), (
        "README.md must link to TRADEMARKS.md near the license badge, or the policy is unreachable "
        "from the page most readers land on"
    )


def test_contributing_points_contributors_at_naming_policy() -> None:
    """A fork author reads CONTRIBUTING before shipping, not the license appendix."""
    assert "TRADEMARKS.md" in CONTRIBUTING.read_text(encoding="utf-8"), (
        "CONTRIBUTING.md must link to TRADEMARKS.md so people shipping derivative builds find the policy"
    )
