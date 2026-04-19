"""Regression guard: .gitignore patterns must not strip shipped sub-packages.

Hatchling uses VCS-awareness during the wheel build — any path that
`git check-ignore` reports as ignored is dropped from the archive.  Two
back-to-back releases (v1.8.9, v1.8.10) broke because:

- v1.8.9: 18 core/ sub-packages were excluded in ``pyproject.toml``'s
  ``[tool.hatch.build] exclude`` list.  `bernstein --version` crashed
  with ``ModuleNotFoundError: No module named 'bernstein.core.config'``.
- v1.8.10: ``.gitignore`` had ``*token*`` (aimed at stray secret-token
  files) which also matched ``src/bernstein/core/tokens/**``.  ``bernstein
  run`` crashed with ``ModuleNotFoundError: No module named
  'bernstein.core.tokens'``.

This test fails loudly if anyone re-introduces a pattern that would drop
a shipped sub-package.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "bernstein"


def _enumerate_shipped_subpackages() -> list[Path]:
    """Return every sub-package directory under ``src/bernstein/``."""
    packages: list[Path] = []
    for pkg_init in SRC_ROOT.rglob("__init__.py"):
        if "__pycache__" in pkg_init.parts:
            continue
        if pkg_init == SRC_ROOT / "__init__.py":
            continue
        packages.append(pkg_init.parent)
    return packages


def test_no_shipped_subpackage_is_gitignored() -> None:
    """No ``src/bernstein/**/__init__.py`` may be matched by ``.gitignore``.

    If this fails, the wheel will silently ship without the affected
    sub-package and users will see ``ModuleNotFoundError`` on install.
    """
    packages = _enumerate_shipped_subpackages()
    assert packages, "expected to discover at least one bernstein sub-package"

    rel_paths = [str((pkg / "__init__.py").relative_to(REPO_ROOT)) for pkg in packages]

    result = subprocess.run(
        ["git", "check-ignore", "-v", *rel_paths],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )

    # ``git check-ignore`` exits 0 when at least one path is ignored, 1 when
    # none are, and 128 on other errors (e.g. not a repo).  We want 1.
    if result.returncode == 0:
        offending = result.stdout.strip().splitlines()
        msg = (
            "gitignore rule matches shipped src/bernstein/ sub-package(s).  "
            "Hatchling will drop them from the wheel and `pip install` users "
            "will see ModuleNotFoundError.  Offending entries:\n  "
            + "\n  ".join(offending)
        )
        raise AssertionError(msg)
