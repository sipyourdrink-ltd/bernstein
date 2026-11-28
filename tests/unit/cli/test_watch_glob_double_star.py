"""``bernstein watch --glob`` lets ``**/`` match zero directories too (#7875).

The filter passed the relative path to ``fnmatch``, so ``src/**/*.py`` needed at least one directory
between ``src`` and the file: ``src/nested/module.py`` matched and ``src/root.py`` did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.cli.commands.watch_cmd import _DebounceHandler, _matches_glob

ROOT = Path("/repo")


@pytest.mark.parametrize(
    ("rel", "matched"),
    [
        ("src/root.py", True),
        ("src/nested/module.py", True),
        ("src/a/b/deep.py", True),
        ("src/notes.txt", False),
        ("tests/root.py", False),
        ("srcx/root.py", False),
    ],
)
def test_the_documented_example(rel: str, matched: bool) -> None:
    assert _matches_glob(ROOT / rel, ROOT, "src/**/*.py") is matched


@pytest.mark.parametrize(
    ("pattern", "rel", "matched"),
    [
        ("**/*.py", "root.py", True),
        ("**/*.py", "a/b/c.py", True),
        ("src/**/test_*.py", "src/test_root.py", True),
        ("a/**/b/**/c.py", "a/c.py", False),
        ("a/**/b/**/c.py", "a/b/c.py", True),
        ("a/**/b/**/c.py", "a/x/b/y/c.py", True),
    ],
)
def test_each_double_star_segment_matches_zero_or_more_directories(pattern: str, rel: str, matched: bool) -> None:
    assert _matches_glob(ROOT / rel, ROOT, pattern) is matched


@pytest.mark.parametrize(
    ("pattern", "rel", "matched"),
    [("*.py", "root.py", True), ("src/*.py", "src/root.py", True), ("*.txt", "root.py", False), (None, "x.bin", True)],
)
def test_patterns_without_double_star_keep_their_behaviour(pattern: str | None, rel: str, matched: bool) -> None:
    assert _matches_glob(ROOT / rel, ROOT, pattern) is matched


def test_the_watch_filter_passes_a_direct_child_to_the_callback(tmp_path: Path) -> None:
    seen: list[set[Path]] = []
    handler = _DebounceHandler(tmp_path, "src/**/*.py", seen.append, debounce_secs=60)
    handler.push(tmp_path / "src" / "root.py")
    handler.push(tmp_path / "src" / "notes.txt")
    handler._fire()
    assert seen == [{tmp_path / "src" / "root.py"}]
