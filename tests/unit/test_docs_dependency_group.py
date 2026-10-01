"""Guard the ``docs`` dependency-group against drift from ``docs/requirements.in``.

The ``docs`` group in ``pyproject.toml`` (added for #6059) and
``docs/requirements.in`` are two declarations of the same toolchain. The
comment on the group promises the floors mirror the ``.in`` file, but nothing
verified that promise - a future edit could bump one and leave the other, and
``uv run --group docs mkdocs build --strict`` would then resolve a toolchain
the standalone pin file no longer documents. This test pins the two together
so the drift fails here instead of at the next docs build.

``check_docs_requirements_pins.py`` already gates ``.in`` against ``.txt``;
this is the missing ``pyproject.toml`` half of the same invariant.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.whole_tree_guard

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"
REQUIREMENTS_IN = REPO / "docs" / "requirements.in"

#: Lines in a ``.in`` file that declare something other than a requirement
#: (mirrors ``check_docs_requirements_pins._OPTION_PREFIXES``).
_OPTION_PREFIXES = ("-r", "-c", "-e", "--", "-i", "-f")


def _direct_requirements(text: str) -> list[Requirement]:
    """Parse directly-declared requirements from a pip-compile ``.in`` source."""
    requirements: list[Requirement] = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith(_OPTION_PREFIXES):
            continue
        requirements.append(Requirement(line))
    return requirements


def _docs_group() -> list[Requirement]:
    """The ``docs`` dependency-group as parsed requirements."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    groups = data.get("dependency-groups", {})
    assert "docs" in groups, "docs dependency-group is missing from pyproject.toml"
    return [Requirement(item) for item in groups["docs"]]


def _normalised(requirements: list[Requirement]) -> set[str]:
    """Canonical specifier strings so order and whitespace never fake a diff."""
    return {str(requirement) for requirement in requirements}


def test_docs_group_mirrors_requirements_in() -> None:
    """The two toolchain declarations must name the same packages at the same floors."""
    in_text = REQUIREMENTS_IN.read_text(encoding="utf-8")
    assert _normalised(_docs_group()) == _normalised(_direct_requirements(in_text))
