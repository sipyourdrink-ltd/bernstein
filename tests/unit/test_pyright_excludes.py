"""Guard: every ``[tool.pyright] exclude`` entry must match something on disk.

Pyright matches excludes by path. After the core/ reorganisation, 134 of 172
entries named pre-move locations and silently stopped applying (issue #4864).
The advisory error count then measured the rename, not typing health, and the
#345h hygiene list became unauditable.

This test is strict: there is no forward-looking allowlist. An exclude that
matches nothing is a bug — drop it or put the real path back.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyright_excludes() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = data["tool"]["pyright"]["exclude"]
    assert isinstance(excludes, list)
    return [str(entry) for entry in excludes]


def _exclude_matches(root: Path, exclude: str) -> bool:
    """Return True when *exclude* names an existing file or directory under *root*."""
    relative = exclude.rstrip("/")
    target = root / relative
    if exclude.endswith("/"):
        return target.is_dir()
    return target.is_file() or target.is_dir()


def test_every_pyright_exclude_matches_something_on_disk() -> None:
    """No stale exclude may linger after a move or deletion."""
    excludes = _pyright_excludes()
    assert excludes, "[tool.pyright] exclude must not be empty without an explicit decision"
    missing = [entry for entry in excludes if not _exclude_matches(REPO_ROOT, entry)]
    assert missing == [], (
        "These [tool.pyright] exclude entries match nothing on disk. Pyright "
        "treats them as no-ops, so the advisory count and the #345h hygiene "
        "list both lie. Drop each entry or update it to the post-move path:\n  " + "\n  ".join(missing)
    )


def test_pyright_exclude_list_has_no_duplicates() -> None:
    excludes = _pyright_excludes()
    assert len(excludes) == len(set(excludes)), "duplicate [tool.pyright] exclude entries"
