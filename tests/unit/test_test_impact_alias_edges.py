"""The impact selector must not lose the edge a legacy import alias hides.

A package can keep an old dotted import path alive without shipping a physical
shim: it registers a ``sys.meta_path`` finder backed by a
``{short_name: real_dotted_module}`` table, and ``import pkg.old_name`` then
resolves to ``pkg.subpkg.old_name``. Nothing on disk is ever named
``pkg.old_name``.

An import graph keyed on the literal dotted name a file wrote therefore has no
edge from the real source module to a test that imports it under the old name.
The blocking pull_request Test shards run only what that graph selects, so a
change confined to the real module passes the required lane without the suite
written for it ever executing. The push-to-main lane uses full discovery and
does run it, which makes the gap PR-only: the signal is missing exactly where
it is meant to gate.

These tests pin both halves:

* the alias table is discovered from the source tree by shape, not from a
  hardcoded list of package names, so a package that adds a third table is
  picked up without editing the analyser;
* an alias whose legacy name is also a real module on disk is left alone,
  because the redirect finders are appended to ``sys.meta_path`` and the real
  module wins the import.
"""

from __future__ import annotations

from pathlib import Path

# Aliased on import: pytest would otherwise try to collect the class as a test
# suite and warn about its constructor.
from bernstein.core.quality.test_impact import TestImpactAnalyzer as ImpactAnalyzer
from bernstein.core.quality.test_impact import (
    build_compat_dep_map,
    compat_get_affected_tests,
    discover_module_aliases,
    resolve_module_aliases,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _alias_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Build a source tree whose package __init__ declares a redirect table."""
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"

    _write(src / "proj" / "__init__.py", "")
    _write(
        src / "proj" / "core" / "__init__.py",
        '_REDIRECT_MAP: dict[str, str] = {\n    "widget": "proj.core.machinery.widget",\n}\n',
    )
    _write(src / "proj" / "core" / "machinery" / "__init__.py", "")
    _write(src / "proj" / "core" / "machinery" / "widget.py", "VALUE = 1\n")
    # Imports the module under the name that only the redirect table resolves.
    _write(tests / "test_widget_lifecycle.py", "from proj.core.widget import VALUE\n")
    # Imports the real path, so it is selected with or without alias handling.
    _write(tests / "test_widget_direct.py", "from proj.core.machinery.widget import VALUE\n")

    return src, tests, "src/proj/core/machinery/widget.py", "tests/unit/test_widget_lifecycle.py"


def test_alias_table_is_discovered_from_the_source_tree(tmp_path: Path) -> None:
    src, _tests, _changed, _lifecycle = _alias_fixture(tmp_path)
    assert discover_module_aliases(src) == {"proj.core.widget": "proj.core.machinery.widget"}


def test_alias_shadowed_by_a_real_module_is_not_rewritten(tmp_path: Path) -> None:
    """The redirect finders are appended to sys.meta_path, so disk wins."""
    src = tmp_path / "src"
    _write(src / "proj" / "__init__.py", "")
    _write(
        src / "proj" / "core" / "__init__.py",
        '_REDIRECT_MAP = {"widget": "proj.core.machinery.widget"}\n',
    )
    _write(src / "proj" / "core" / "widget.py", "VALUE = 1\n")
    _write(src / "proj" / "core" / "machinery" / "__init__.py", "")
    _write(src / "proj" / "core" / "machinery" / "widget.py", "VALUE = 2\n")

    assert discover_module_aliases(src) == {}


def test_alias_target_that_does_not_exist_is_dropped(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(src / "proj" / "__init__.py", "")
    _write(src / "proj" / "core" / "__init__.py", '_REDIRECT_MAP = {"gone": "proj.core.deleted.gone"}\n')

    assert discover_module_aliases(src) == {}


def test_resolution_keeps_the_legacy_name_alongside_the_real_one() -> None:
    aliases = {"proj.core.widget": "proj.core.machinery.widget"}
    assert resolve_module_aliases({"proj.core.widget"}, aliases) == {
        "proj.core.widget",
        "proj.core.machinery.widget",
    }


def test_resolution_terminates_on_a_cyclic_alias_table() -> None:
    aliases = {"a.x": "a.y", "a.y": "a.x"}
    assert resolve_module_aliases({"a.x"}, aliases) == {"a.x", "a.y"}


def test_compat_selector_picks_the_alias_importing_test(tmp_path: Path) -> None:
    src, tests, changed, lifecycle = _alias_fixture(tmp_path)
    dep_map = build_compat_dep_map(tmp_path, src, [tests], {"proj"})

    selected = {
        p.relative_to(tmp_path).as_posix()
        for p in compat_get_affected_tests([changed], dep_map, root=tmp_path, src_root=src)
    }

    assert lifecycle in selected, (
        "a change confined to the real module must select the suite that imports it under the legacy alias"
    )
    assert "tests/unit/test_widget_direct.py" in selected


def test_analyzer_picks_the_alias_importing_test(tmp_path: Path) -> None:
    src, tests, changed, lifecycle = _alias_fixture(tmp_path)
    analyzer = ImpactAnalyzer(
        tmp_path,
        cache_path=tmp_path / "cache.json",
        src_root=src,
        test_dirs=[tests],
    )

    analysis = analyzer.analyze([changed])

    assert not analysis.fallback_used
    assert lifecycle in analysis.affected_tests


def test_repo_worktree_change_selects_its_lifecycle_suite() -> None:
    """Regression pin on the reported case, against the real source tree.

    ``tests/unit/test_worktree_lifecycle.py`` imports ``bernstein.core.worktree``,
    which exists only in the redirect table; the file on disk is
    ``src/bernstein/core/git/worktree.py``.
    """
    src = _REPO_ROOT / "src"
    changed = "src/bernstein/core/git/worktree.py"
    lifecycle = "tests/unit/test_worktree_lifecycle.py"
    assert (_REPO_ROOT / changed).is_file()
    assert (_REPO_ROOT / lifecycle).is_file()

    aliases = discover_module_aliases(src)
    assert aliases.get("bernstein.core.worktree") == "bernstein.core.git.worktree"

    dep_map = build_compat_dep_map(_REPO_ROOT, src, [_REPO_ROOT / "tests" / "unit"], {"bernstein"})
    selected = {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in compat_get_affected_tests([changed], dep_map, root=_REPO_ROOT, src_root=src)
    }

    assert lifecycle in selected
