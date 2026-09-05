"""A guard that scans a directory must be selected by the diff that breaks it.

A structural guard reads its subject instead of importing it, so the import
graph offers no edge and ``extract_path_literals`` recovers one from the paths
the guard names. That recovery stops at guards that name a *file*. A guard that
walks a *directory* names nothing a changed path can match: it holds a root
built from path segments and hands ``"*.py"`` to ``rglob``.

The consequence is not an abstract missing edge. ``test_cast_alias_form`` walks
every module under ``src/bernstein`` and fails on a new one that declares a
string-valued cast alias. Nothing selected it for that diff, so such a PR was
green in its own lane and failed for the first time in the merge queue, where
the failure blocks every entry behind it.

The edge is declared rather than inferred. Harvesting every glob-shaped string
was measured first and rejected: ``"src/*.py"`` appears in eight tests as
fixture data for a path-matching rule under test, and honouring those bound
thirteen unrelated tests to every change under ``src``.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.quality.test_impact import (
    build_compat_dep_map,
    compat_get_affected_tests,
    extract_scanned_trees,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _scanner_fixture(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """A guard whose module body is ``body``, importing nothing from the tree."""
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"

    _write(src / "proj" / "__init__.py", "")
    _write(src / "proj" / "core" / "__init__.py", "")
    _write(src / "proj" / "core" / "widget.py", "VALUE = 1\n")
    _write(tests / "test_tree_scan.py", f"{body}\n\n\ndef test_it() -> None:\n    assert True\n")
    return src, tests


def _selected(tmp_path: Path, src: Path, tests: Path, changed: str) -> set[str]:
    dep_map = build_compat_dep_map(tmp_path, src, [tests], {"proj"})
    return {
        p.relative_to(tmp_path).as_posix()
        for p in compat_get_affected_tests([changed], dep_map, root=tmp_path, src_root=src)
    }


def test_a_guard_declaring_the_tree_it_scans_is_selected_for_a_file_inside_it(tmp_path: Path) -> None:
    """The edge this map exists to provide, for a directory-shaped subject."""
    src, tests = _scanner_fixture(tmp_path, 'SCANNED_TREE = "src/proj/core/*.py"')

    assert "tests/unit/test_tree_scan.py" in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_a_declared_tree_that_excludes_the_change_is_not_selected(tmp_path: Path) -> None:
    """Negative control: the match must be the glob, not the presence of one."""
    src, tests = _scanner_fixture(tmp_path, 'SCANNED_TREE = "src/proj/adapters/*.py"')

    assert "tests/unit/test_tree_scan.py" not in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_a_glob_shaped_fixture_string_does_not_create_an_edge(tmp_path: Path) -> None:
    """The reason this edge is declared rather than inferred.

    ``"src/*.py"`` is fixture data in eight tests in this repository. Binding a
    test to a tree because it mentions a pattern would enrol it in every change
    under that tree, and would keep doing so as fixtures are added.
    """
    src, tests = _scanner_fixture(tmp_path, 'PATTERN_UNDER_TEST = "src/proj/core/*.py"')

    assert "tests/unit/test_tree_scan.py" not in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_a_recursive_declaration_reaches_a_module_directly_inside_the_tree(tmp_path: Path) -> None:
    """``**/`` matches zero directories for ``pathlib``, so it must here too.

    Otherwise a guard declaring the exact string it passes to ``glob()`` covers
    every nested module and silently misses the ones sitting directly in the
    package -- the shallowest files, and the easiest to get wrong.
    """
    src, tests = _scanner_fixture(tmp_path, 'SCANNED_TREE = "src/proj/**/*.py"')
    _write(src / "proj" / "shallow.py", "VALUE = 2\n")

    assert "tests/unit/test_tree_scan.py" in _selected(tmp_path, src, tests, "src/proj/shallow.py")


def test_a_recursive_declaration_still_reaches_a_nested_module(tmp_path: Path) -> None:
    """The written form must keep working alongside its collapsed variant."""
    src, tests = _scanner_fixture(tmp_path, 'SCANNED_TREE = "src/proj/**/*.py"')

    assert "tests/unit/test_tree_scan.py" in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_several_trees_may_be_declared_at_once(tmp_path: Path) -> None:
    """A guard that walks more than one top declares them as a sequence."""
    src, tests = _scanner_fixture(tmp_path, 'SCANNED_TREES = ["scripts/*.py", "src/proj/core/*.py"]')

    assert "tests/unit/test_tree_scan.py" in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_a_leading_underscore_does_not_hide_the_declaration(tmp_path: Path) -> None:
    """A module may keep its own private-name convention."""
    src, tests = _scanner_fixture(tmp_path, '_SCANNED_TREE = "src/proj/core/*.py"')

    assert "tests/unit/test_tree_scan.py" in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_a_declaration_inside_a_function_is_not_harvested(tmp_path: Path) -> None:
    """Only a module-level binding declares the subject of the whole file."""
    body = 'def _helper() -> str:\n    SCANNED_TREE = "src/proj/core/*.py"\n    return SCANNED_TREE'
    src, tests = _scanner_fixture(tmp_path, body)

    assert "tests/unit/test_tree_scan.py" not in _selected(tmp_path, src, tests, "src/proj/core/widget.py")


def test_extraction_ignores_a_non_string_declaration(tmp_path: Path) -> None:
    """A malformed declaration must not crash the map build."""
    _write(tmp_path / "t.py", "SCANNED_TREE = 3\nSCANNED_TREES = [1, 'src/proj/*.py']\n")

    assert extract_scanned_trees(tmp_path / "t.py") == {"src/proj/*.py"}


def test_a_new_source_module_selects_the_repo_tree_guards() -> None:
    """Regression pin on the reported case, against the real source tree.

    ``test_cast_alias_form`` fails on a new module anywhere under
    ``src/bernstein`` that declares ``_CAST_X = "..."``. Before the declared
    glob edge existed the selector returned it for no such diff at all.
    """
    src_root = _REPO_ROOT / "src"
    dep_map = build_compat_dep_map(_REPO_ROOT, src_root, [_REPO_ROOT / "tests" / "unit"])

    selected = {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in compat_get_affected_tests(
            ["src/bernstein/core/security/zz_probe.py"],
            dep_map,
            root=_REPO_ROOT,
            src_root=src_root,
        )
    }

    assert "tests/unit/test_cast_alias_form.py" in selected
    assert "tests/unit/test_shipped_tree_imports_only_shipped_code.py" in selected
    assert "tests/unit/test_core_has_no_directory_vendor_sdk.py" in selected


def test_an_unrelated_change_does_not_select_the_source_tree_guards() -> None:
    """The declared trees must still bound the edge against the real repo."""
    src_root = _REPO_ROOT / "src"
    dep_map = build_compat_dep_map(_REPO_ROOT, src_root, [_REPO_ROOT / "tests" / "unit"])

    selected = {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in compat_get_affected_tests(
            ["docs/index.md"],
            dep_map,
            root=_REPO_ROOT,
            src_root=src_root,
        )
    }

    assert "tests/unit/test_cast_alias_form.py" not in selected
