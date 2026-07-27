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

These tests pin three things:

* a table is recognised by what it contains, not by what it is bound to. The
  name is private to the package and unobservable from an importer, so keying
  discovery on it lets a rename delete selection edges with nothing failing;
* an alias whose legacy name is also a real module on disk is left alone,
  because the redirect finders are appended to ``sys.meta_path`` and the real
  module wins the import;
* whatever the reader manages to see statically agrees with what the running
  interpreter actually serves. The first two are rules about the shapes we
  read today; the last is the property those rules exist to deliver, checked
  against the finders themselves so a table we cannot read is a failure rather
  than a silent omission.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

# Imported for their side effect: each package __init__ appends its redirect
# finder to sys.meta_path, which is what the reconciliation test interrogates.
import bernstein.cli
import bernstein.core  # noqa: F401

# Aliased on import: pytest would otherwise try to collect the class as a test
# suite and warn about its constructor.
from bernstein.core.quality.test_impact import TestImpactAnalyzer as ImpactAnalyzer
from bernstein.core.quality.test_impact import (
    _real_module_names,
    build_compat_dep_map,
    compat_get_affected_tests,
    discover_module_aliases,
    resolve_module_aliases,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _alias_fixture(tmp_path: Path, table_name: str = "_REDIRECT_MAP") -> tuple[Path, Path, str, str]:
    """Build a source tree whose package __init__ declares a redirect table.

    ``table_name`` is the identifier the table is bound to. Nothing in the
    import system cares what it is called, so discovery must not either.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"

    _write(src / "proj" / "__init__.py", "")
    _write(
        src / "proj" / "core" / "__init__.py",
        f'{table_name}: dict[str, str] = {{\n    "widget": "proj.core.machinery.widget",\n}}\n',
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


def test_alias_table_bound_to_any_name_is_discovered(tmp_path: Path) -> None:
    """Discovery must key on the table's shape, not on what it is called.

    The import system resolves a redirect through the finder that reads the
    table. The identifier the table is bound to is private to the package and
    never observable from an importer, so a rename is a refactor no reviewer
    would question. Keying discovery on the name makes that refactor delete
    test-selection edges with nothing turning red.
    """
    src, _tests, _changed, _lifecycle = _alias_fixture(tmp_path, table_name="_LEGACY_MODULES")
    assert discover_module_aliases(src) == {"proj.core.widget": "proj.core.machinery.widget"}


def test_selector_picks_the_alias_test_when_the_table_is_named_differently(tmp_path: Path) -> None:
    """The end-to-end consequence of the rule above, at selector level."""
    src, tests, changed, lifecycle = _alias_fixture(tmp_path, table_name="_LEGACY_MODULES")
    dep_map = build_compat_dep_map(tmp_path, src, [tests], {"proj"})

    selected = {
        p.relative_to(tmp_path).as_posix()
        for p in compat_get_affected_tests([changed], dep_map, root=tmp_path, src_root=src)
    }

    assert lifecycle in selected, (
        "a change confined to the real module must select the suite that imports it under the "
        "legacy alias, whatever the redirect table happens to be called"
    )


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


def _live_redirect_edges() -> dict[str, str]:
    """Return the alias edges the installed finders actually serve.

    Read from the running interpreter rather than from the source text: every
    finder ``bernstein`` put on ``sys.meta_path`` is asked, by calling it,
    which legacy names it resolves. Candidate keys come from iterating the
    defining module's globals for ``str -> str`` mappings, with no filter on
    what those mappings are named, and ``find_spec`` decides which of them are
    really redirect keys. An unrelated ``str -> str`` global is rejected
    because the finder declines it, not because of how it is spelled.
    """
    edges: dict[str, str] = {}
    for finder in list(sys.meta_path):
        finder_cls = type(finder)
        defining_module = sys.modules.get(getattr(finder_cls, "__module__", "") or "")
        prefix = getattr(finder_cls, "_PREFIX", None)
        if defining_module is None or not isinstance(prefix, str):
            continue
        if not getattr(defining_module, "__name__", "").startswith("bernstein"):
            continue
        for candidate in list(vars(defining_module).values()):
            if not isinstance(candidate, dict):
                continue
            for short, target in cast("dict[object, object]", candidate).items():
                if not isinstance(short, str) or not isinstance(target, str):
                    continue
                legacy = f"{prefix}{short}"
                if finder.find_spec(legacy, None, None) is not None:
                    edges[legacy] = target
    return edges


def test_every_live_redirect_edge_is_discovered() -> None:
    """Reconcile what the import system serves against what the analyser sees.

    The tests above pin the shapes of table that discovery reads today. This
    one pins the property those shapes exist to deliver, and it does so
    without reference to any of them: whatever a package does to install a
    redirect, if the running interpreter resolves a legacy name through it,
    the selector has to know about that edge, or a change to the real module
    silently stops selecting the suites that import it under the legacy name.

    Reading the tables statically is deliberate in ``discover_module_aliases``
    and stays that way; it keeps discovery free of import side effects and
    usable on a tree that is not installed. The cost of that choice is that
    the reader can drift from the importer. This is the test that notices.
    """
    src = _REPO_ROOT / "src"
    real_modules = _real_module_names(src)

    # Same two exclusions discover_module_aliases applies, for a like-for-like
    # comparison: a legacy name that is also a real module loses to disk
    # because the finders are appended to sys.meta_path, and an edge pointing
    # outside the source tree has no source file to hang a dependency on.
    live = {
        legacy: target
        for legacy, target in _live_redirect_edges().items()
        if legacy not in real_modules and target in real_modules
    }
    assert live, "no redirect finder was live, so this test would prove nothing"

    discovered = discover_module_aliases(src)
    missing = sorted(legacy for legacy in live if legacy not in discovered)

    assert not missing, (
        f"{len(missing)} legacy import names resolve at runtime but are invisible to the test "
        f"selector, so a change to the module behind them would not select the suites that "
        f"import them under the legacy name: {missing[:10]}"
    )
