"""The README ships inside the wheel, so it has to work away from the repo.

`pyproject.toml` sets `readme = "README.md"`, which makes this file the
package's long description. PyPI renders that description standalone: there is
no repository around it, so a link written as `docs/getting-started/install.md`
resolves against `https://pypi.org/project/bernstein/` and 404s.

Nothing caught this when the README was rebuilt as a front page. The previous
README was one self-contained 70KB document that mostly linked outward; the
front-page rewrite replaced it with a short page whose whole job is pointing at
files elsewhere in the repository, and every one of those pointers was written
in the form that only works on github.com.

These tests fix the packaged artefact against the surface it is published on,
rather than against the surface it is authored on.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

LINK = re.compile(r"\]\(([^)]+)\)")
OFF_REPO_SAFE = ("http://", "https://", "#", "mailto:")


def _links() -> list[str]:
    return LINK.findall(README.read_text(encoding="utf-8"))


def _project() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project")
    assert isinstance(project, dict)
    return project


def test_readme_is_the_packaged_long_description() -> None:
    """The premise of every other test here. If this changes, they stop mattering."""
    assert _project().get("readme") == "README.md"


def test_no_readme_link_depends_on_the_repository_around_it() -> None:
    relative = [link for link in _links() if not link.startswith(OFF_REPO_SAFE)]
    assert not relative, (
        "these README links resolve only inside the repository, so they 404 on "
        f"PyPI where this file is the project description: {sorted(set(relative))}"
    )


def test_readme_absolute_links_point_at_paths_this_repo_actually_has() -> None:
    """An absolute link is not automatically a working one.

    Rewriting a relative path to a blob URL is a mechanical edit, and a typo in
    it fails in exactly the place nobody looks: rendered on PyPI, months later.
    Checking the path component against the working tree catches that here
    instead, without needing the network.
    """
    blob = "https://github.com/sipyourdrink-ltd/bernstein/blob/main/"
    broken: list[str] = []
    for link in _links():
        if not link.startswith(blob):
            continue
        path = link[len(blob) :].split("#", 1)[0]
        if not (REPO_ROOT / path).exists():
            broken.append(link)
    assert not broken, f"README links to paths this repository does not contain: {broken}"


def test_python_classifiers_match_the_versions_the_project_supports() -> None:
    """A classifier is what package indexes filter on, so a stale one hides the project.

    `requires-python` is the enforced floor and the CI matrix is the tested set;
    the classifier list is the advertised one, and it drifts silently because
    nothing fails when it lags.
    """
    classifiers = _project().get("classifiers")
    assert isinstance(classifiers, list)
    advertised = {
        item.rsplit(" :: ", 1)[-1]
        for item in classifiers
        if isinstance(item, str) and item.startswith("Programming Language :: Python :: 3.")
    }

    # Only `python-version:` assignments count as the tested set. Scanning the
    # whole file for version-shaped text reads prose as configuration: ci.yml
    # carries the comment "3.11 is intentionally excluded", and a loose scan
    # turns that sentence into a demand to advertise 3.11, which
    # `requires-python = ">=3.12"` refuses to install on.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    tested: set[str] = set()
    for line in ci.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("python-version:"):
            continue
        tested.update(re.findall(r"3\.\d+", stripped))

    assert tested, "expected to find python-version assignments in ci.yml"
    assert tested <= advertised, (
        f"CI tests Python {sorted(tested - advertised)} but the classifiers do not "
        "advertise it, so index filters exclude the project from those searches"
    )
