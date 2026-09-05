"""Every test that scans the source tree declares itself a whole-tree guard.

A whole-tree guard asserts an invariant over the tree by walking it -- no
module under ``core/`` is unreachable, exactly one receipt-verify protocol
exists -- rather than by importing the code it checks. The affected-set
selector builds its map from import edges, so no diff ever produces one to a
guard: a pull request that adds the very thing a guard forbids runs green on
its own checks and reds in the merge group instead, where the failure costs an
ejection and takes every entry queued behind it (#5428).

``scripts/run_tests.py`` selects marked files on every run. That only holds if
the set is declared rather than inferred, so this test fails when a
tree-scanning test is added without the marker.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from run_tests import discover_whole_tree_guard_files

#: This file is itself a guard: it walks `tests/` and fails when a new
#: tree-scanning test arrives unmarked, which is exactly the change no import
#: edge would connect it to. Marked explicitly rather than left to be matched
#: by the marker names quoted in its own assertions (#5428).
pytestmark = pytest.mark.whole_tree_guard

#: Suites searched for unmarked guards -- the two the selector indexes.
SEARCH_DIRS = ("tests/unit", "tests/integration")

#: Call names that walk a directory tree.
_WALK_CALLS = frozenset({"rglob", "glob", "walk_packages", "iter_modules"})

#: Spellings of the source root inside a path expression.
_SRC_MARKERS = ('"src"', "'src'", '"src/bernstein"', "'src/bernstein'")

#: Tree-scanning tests that are deliberately not guards, with the reason.
EXEMPT: dict[str, str] = {
    "tests/unit/test_whole_tree_guards_are_marked.py": (
        "this file -- it walks tests/, not src/, and is itself the registry"
    ),
}


def _source_root_names(tree: ast.Module, source: str) -> set[str]:
    """Module-level names bound to a path built from the source root.

    Matched on the assigned expression's own text, so both `Path("src")` and
    `parents[2] / "src" / "bernstein"` are recognised.
    """
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        if node.value is None:
            continue
        segment = ast.get_source_segment(source, node.value) or ""
        if not any(marker in segment for marker in _SRC_MARKERS):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _receiver_root(node: ast.expr) -> str | None:
    """The base name of `A.b[0] / "c"`, so a walk can be traced to its root."""
    while True:
        if isinstance(node, ast.Attribute | ast.Subscript):
            node = node.value
        elif isinstance(node, ast.BinOp):
            node = node.left
        else:
            break
    return node.id if isinstance(node, ast.Name) else None


def _scans_the_source_tree(path: Path) -> bool:
    """True when the module walks a path it built from the source root.

    Deliberately narrower than "mentions src and globs something". A test that
    globs its own `tmp_path` fixture cannot be broken by a source change, so
    it is not a guard and marking it would only slow every pull request down.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):  # pragma: no cover - unparseable is not a guard
        return False

    roots = _source_root_names(tree, source)
    if not roots:
        return False

    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _WALK_CALLS
        and _receiver_root(node.func.value) in roots
        for node in ast.walk(tree)
    )


def _candidate_files() -> list[Path]:
    """Every test file under the indexed suites, repo-relative."""
    found: list[Path] = []
    for search_dir in SEARCH_DIRS:
        directory = REPO_ROOT / search_dir
        if directory.is_dir():
            found.extend(p.relative_to(REPO_ROOT) for p in directory.rglob("test_*.py"))
    return sorted(found)


def test_every_tree_scanning_test_carries_the_marker() -> None:
    """The registry cannot be inferred, so it has to be declared.

    An unmarked guard is invisible to `--affected`: it will not run on the
    pull request that breaks it, and the first anyone hears of it is a red
    merge group.
    """
    marked = {str(p) for p in discover_whole_tree_guard_files(REPO_ROOT)}
    unmarked = sorted(
        str(path)
        for path in _candidate_files()
        if str(path) not in marked and str(path) not in EXEMPT and _scans_the_source_tree(path)
    )
    assert unmarked == [], (
        "these tests scan the source tree but do not carry "
        "`pytestmark = pytest.mark.whole_tree_guard`:\n  " + "\n  ".join(unmarked) + "\n\n"
        "Without it `scripts/run_tests.py --affected` cannot select them, so "
        "they only run in the merge group. Add the marker, or add the file to "
        "EXEMPT with the reason its scan cannot be broken by a source change."
    )


def test_the_known_guards_are_discovered() -> None:
    """The two guards on main are found, so discovery is not vacuously empty.

    A registry that silently returns nothing would satisfy every other
    assertion here while selecting no guard at all.
    """
    discovered = {str(p) for p in discover_whole_tree_guard_files(REPO_ROOT)}
    assert "tests/unit/test_receipt_verify_single_protocol.py" in discovered
    assert "tests/unit/test_verify_result_field_shape_collisions.py" in discovered


def test_discovery_reads_source_text_not_imports(tmp_path: Path) -> None:
    """A guard that fails to import must still be selected.

    Discovery runs before pytest starts. Importing each candidate to look for
    the marker would drop exactly the guard whose module is broken, which is
    the one whose failure most needs to be seen.
    """
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    broken = tmp_path / "tests" / "unit" / "test_broken_guard.py"
    broken.write_text(
        "import pytest\npytestmark = pytest.mark.whole_tree_guard\nimport does_not_exist\n",
        encoding="utf-8",
    )
    assert discover_whole_tree_guard_files(tmp_path) == [Path("tests/unit/test_broken_guard.py")]


def test_an_unmarked_scanner_is_reported(tmp_path: Path) -> None:
    """The detector fires on the shape it exists to catch."""
    scanner = tmp_path / "test_scanner.py"
    scanner.write_text(
        'from pathlib import Path\n\nSRC = Path(__file__).parents[2] / "src" / "bernstein"\n\n\n'
        "def test_x() -> None:\n    assert list(SRC.rglob('*.py')) is not None\n",
        encoding="utf-8",
    )
    assert _scans_the_source_tree(scanner) is True


@pytest.mark.parametrize(
    "body",
    [
        'from pathlib import Path\n\nSRC = Path("src")\n\n\ndef test_x() -> None:\n    assert SRC.exists()\n',
        'from pathlib import Path\n\nSRC = Path("src")\n\n\ndef test_x(tmp_path: Path) -> None:\n'
        "    assert list(tmp_path.rglob('*.py')) == []\n",
    ],
)
def test_a_test_that_does_not_walk_the_source_tree_is_not_flagged(tmp_path: Path, body: str) -> None:
    """Naming the source root is not walking it, and walking a fixture is not either.

    The second case is the one that matters: a test that globs its own
    `tmp_path` cannot be broken by a source change, so marking it would cost
    every pull request time for no coverage.
    """
    candidate = tmp_path / "test_candidate.py"
    candidate.write_text(body, encoding="utf-8")
    assert _scans_the_source_tree(candidate) is False
