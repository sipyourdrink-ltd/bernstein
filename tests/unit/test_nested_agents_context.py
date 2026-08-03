"""Guard: nested per-directory AGENTS.md files stay small, accurate, mapped.

The root ``AGENTS.md`` and its mirrors are generated and drift-gated, but
they are repo-global. Nested per-directory ``AGENTS.md`` files carry the
curated part the generator cannot derive - subtree invariants, testing
commands, gotchas - for the directories where getting those wrong is
expensive (issue #3372).

Curated prose rots in three specific ways, and each one is pinned here so
it fails CI instead of surfacing as an agent acting on a stale claim:

1. A file grows past the point anyone reads it (line budget).
2. A file references a path that was since renamed or deleted.
3. A directory on the load-bearing list loses its file, or a new nested
   file never makes it onto the root map (the generator derives the map
   from the tree, so the mirror-sync guard covers the render; this file
   covers the tree itself).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories that must carry a nested AGENTS.md. Adding coverage means
#: adding the directory here and creating the file; removing one is a
#: deliberate act that must show up in a reviewed diff.
REQUIRED_DIRS: tuple[str, ...] = (
    "src/bernstein/adapters",
    "src/bernstein/cli",
    "src/bernstein/core/lineage",
    "src/bernstein/core/orchestration",
    "src/bernstein/core/quality",
    "src/bernstein/core/security",
    "tests",
)

#: A nested context file that needs more than this is trying to be a
#: README. Split the content or cut it.
MAX_LINES = 40

#: Mirrors ``_DIRECTORY_CONTEXT_ROOTS`` in agents_md_generator.py.
SEARCH_ROOTS: tuple[str, ...] = ("src", "tests")

#: Repo-root prefixes under which a backticked reference must resolve.
_CHECKABLE_PREFIXES = ("src/", "tests/", "docs/", "scripts/", "templates/")

_BACKTICK_TOKEN = re.compile(r"`([A-Za-z0-9_.\-/]+)`")


def _nested_agents_files() -> list[Path]:
    out: list[Path] = []
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            out.extend(sorted(base.rglob("AGENTS.md")))
    return out


def _rel_id(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_required_directories_are_covered() -> None:
    """The load-bearing subtrees each carry a context file."""
    missing = [d for d in REQUIRED_DIRS if not (REPO_ROOT / d / "AGENTS.md").is_file()]
    assert not missing, f"directories missing a nested AGENTS.md: {missing}"


@pytest.mark.parametrize("path", _nested_agents_files(), ids=_rel_id)
def test_stays_within_line_budget(path: Path) -> None:
    """Nested context is a briefing, not a README."""
    n_lines = len(path.read_text(encoding="utf-8").splitlines())
    assert n_lines <= MAX_LINES, (
        f"{path.relative_to(REPO_ROOT)} is {n_lines} lines (max {MAX_LINES}); cut or split the content"
    )


@pytest.mark.parametrize("path", _nested_agents_files(), ids=_rel_id)
def test_starts_with_h1(path: Path) -> None:
    """The H1 doubles as the file's description in the generated root map."""
    first = next((ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()), "")
    assert first.startswith("# "), f"{path.relative_to(REPO_ROOT)} must start with a markdown H1"


@pytest.mark.parametrize("path", _nested_agents_files(), ids=_rel_id)
def test_referenced_paths_resolve(path: Path) -> None:
    """A context file naming a renamed or deleted path is a factual error.

    Checks two reference shapes: repo-root-relative (``src/...``) and
    directory-relative (``foo.py``, ``sub/``, ``../sibling/bar.py``).
    Tokens with glob characters or without a path shape are ignored.
    """
    text = path.read_text(encoding="utf-8")
    dangling: list[str] = []
    for token in _BACKTICK_TOKEN.findall(text):
        if "*" in token:
            continue
        if token.startswith(_CHECKABLE_PREFIXES):
            if not (REPO_ROOT / token).exists():
                dangling.append(token)
        elif (
            re.fullmatch(r"(\.\./)*[A-Za-z0-9_\-/]+\.(py|md)", token) or re.fullmatch(r"[A-Za-z0-9_\-]+/", token)
        ) and not ((path.parent / token).exists() or (REPO_ROOT / token).exists()):
            # Directory-relative code/doc reference (``base.py``,
            # ``review_pipeline/``, ``../tasks/task_lifecycle.py``).
            dangling.append(token)
    assert not dangling, f"{path.relative_to(REPO_ROOT)} references paths that do not exist: {dangling}"


def test_root_map_lists_every_nested_file() -> None:
    """The generated root AGENTS.md must map every nested file (and vice
    versa the generator derives the map from the tree, so a stale map is
    also caught by the mirror-sync guard)."""
    root_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    unmapped = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _nested_agents_files()
        if f"`{p.relative_to(REPO_ROOT).as_posix()}`" not in root_text
    ]
    assert not unmapped, f"nested AGENTS.md missing from the root map (run `bernstein agents-md sync`): {unmapped}"
