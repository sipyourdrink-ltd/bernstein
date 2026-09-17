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
    _resolve_relative_import,
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


# ---------------------------------------------------------------------------
# Re-exports (#5111 slice 4)
# ---------------------------------------------------------------------------
#
# The alias tests above cover a name the import SYSTEM redirects. A re-export is
# the ordinary-Python cousin: module A binds a name defined in module B, a test
# imports only A, and a change confined to B still has to select that test.
#
# It does today -- the edge is carried by A's own import of B, so the transitive
# walk reaches it. That is worth pinning rather than assuming: the shape is
# indistinguishable from the alias case to a reader of the test file, the whole
# selection is invisible when it is wrong (a green required lane that ran
# nothing), and nothing else asserts it explicitly.


def _reexport_fixture(tmp_path: Path, facade_body: str, test_body: str) -> tuple[Path, Path]:
    """A project where the test imports a facade and never names the definition."""
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"
    _write(src / "proj" / "__init__.py", "")
    _write(src / "proj" / "engine.py", "def compute() -> int:\n    return 1\n")
    _write(src / "proj" / "facade.py", facade_body)
    _write(tests / "test_via_facade.py", test_body)
    return src, tests


def _selected_for(tmp_path: Path, src: Path, tests: Path, changed: str) -> set[str]:
    dep_map = build_compat_dep_map(tmp_path, src, [tests], {"proj"})
    return {
        p.relative_to(tmp_path).as_posix()
        for p in compat_get_affected_tests([changed], dep_map, root=tmp_path, src_root=src)
    }


def test_a_named_reexport_makes_its_importers_affected(tmp_path: Path) -> None:
    src, tests = _reexport_fixture(
        tmp_path,
        facade_body="from proj.engine import compute\n\n__all__ = ['compute']\n",
        test_body="from proj.facade import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    selected = _selected_for(tmp_path, src, tests, "src/proj/engine.py")
    assert "tests/unit/test_via_facade.py" in selected, (
        "a change to the module that DEFINES a re-exported name must select the suite that "
        "imports it through the facade; the test never names proj.engine"
    )


def test_a_star_reexport_makes_its_importers_affected(tmp_path: Path) -> None:
    """``from x import *`` binds no name statically, so it is the shape most likely to be lost."""
    src, tests = _reexport_fixture(
        tmp_path,
        facade_body="from proj.engine import *  # noqa: F403\n",
        test_body="from proj.facade import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    assert "tests/unit/test_via_facade.py" in _selected_for(tmp_path, src, tests, "src/proj/engine.py")


def test_a_package_init_reexport_makes_its_importers_affected(tmp_path: Path) -> None:
    """The commonest form in this codebase: ``from pkg import name``."""
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"
    _write(src / "proj" / "__init__.py", "from proj.engine import compute\n")
    _write(src / "proj" / "engine.py", "def compute() -> int:\n    return 1\n")
    _write(
        tests / "test_via_facade.py",
        "from proj import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    assert "tests/unit/test_via_facade.py" in _selected_for(tmp_path, src, tests, "src/proj/engine.py")


def test_a_two_hop_reexport_chain_still_reaches_the_test(tmp_path: Path) -> None:
    """Definition -> middle -> facade -> test. One missing hop selects nothing."""
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"
    _write(src / "proj" / "__init__.py", "")
    _write(src / "proj" / "engine.py", "def compute() -> int:\n    return 1\n")
    _write(src / "proj" / "mid.py", "from proj.engine import compute\n")
    _write(src / "proj" / "facade.py", "from proj.mid import compute\n")
    _write(
        tests / "test_via_facade.py",
        "from proj.facade import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    assert "tests/unit/test_via_facade.py" in _selected_for(tmp_path, src, tests, "src/proj/engine.py")


def test_an_unrelated_module_does_not_select_the_facade_test(tmp_path: Path) -> None:
    """The control. Without it every assertion above passes on a selector that returns everything."""
    src, tests = _reexport_fixture(
        tmp_path,
        facade_body="from proj.engine import compute\n",
        test_body="from proj.facade import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    _write(src / "proj" / "unrelated.py", "VALUE = 2\n")
    assert _selected_for(tmp_path, src, tests, "src/proj/unrelated.py") == set()


# ---------------------------------------------------------------------------
# Relative-import re-exports (#5111 slice 4, continued)
# ---------------------------------------------------------------------------
#
# Every re-export fixture above writes the facade's import as an absolute
# dotted path (``from proj.engine import compute``). That is not the form an
# intra-package re-export is normally written in -- ``from .engine import
# compute`` or, from a package's own ``__init__.py``, ``from .engine import
# compute`` again with the package itself as the base -- and the analyser's
# import extraction never resolved ``node.level``, so a relative import's
# module name never carried the package prefix the selector matches on. The
# edge from the defining module through a relatively-imported facade was
# silently dropped: not routed to the full-suite fallback (a separate,
# unrelated test already covers the facade module directly, so the "some
# source changed maps to no test" fail-open path never triggers), just
# missing from the precise selection with nothing failing.


def test_a_relative_reexport_makes_its_importers_affected(tmp_path: Path) -> None:
    """``from .engine import compute`` -- the ordinary sibling-module re-export."""
    src, tests = _reexport_fixture(
        tmp_path,
        facade_body="from .engine import compute\n",
        test_body="from proj.facade import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    # A second, direct-import test so a change to engine.py is already mapped
    # through it and the fail-open "unmapped source" fallback cannot mask the
    # missing edge by selecting everything anyway.
    _write(tests / "test_engine_direct.py", "from proj.engine import compute\n")
    assert _selected_for(tmp_path, src, tests, "src/proj/engine.py") == {
        "tests/unit/test_via_facade.py",
        "tests/unit/test_engine_direct.py",
    }


def test_a_package_init_relative_reexport_makes_its_importers_affected(tmp_path: Path) -> None:
    """``proj/__init__.py`` doing ``from .engine import compute``.

    A package's own ``__init__.py`` is its own ``__package__`` for relative
    resolution -- level 1 refers to ``proj`` itself, not ``proj``'s parent --
    which is the distinction :func:`_resolve_relative_import` has to get
    right for this, the commonest re-export shape in this codebase, to work.
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"
    _write(src / "proj" / "__init__.py", "from .engine import compute\n")
    _write(src / "proj" / "engine.py", "def compute() -> int:\n    return 1\n")
    _write(
        tests / "test_via_facade.py",
        "from proj import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    _write(tests / "test_engine_direct.py", "from proj.engine import compute\n")
    assert _selected_for(tmp_path, src, tests, "src/proj/engine.py") == {
        "tests/unit/test_via_facade.py",
        "tests/unit/test_engine_direct.py",
    }


def test_a_two_level_relative_reexport_makes_its_importers_affected(tmp_path: Path) -> None:
    """``from ..engine import compute`` -- climbing one level up from a subpackage."""
    src = tmp_path / "src"
    tests = tmp_path / "tests" / "unit"
    _write(src / "proj" / "__init__.py", "")
    _write(src / "proj" / "engine.py", "def compute() -> int:\n    return 1\n")
    _write(src / "proj" / "sub" / "__init__.py", "")
    _write(src / "proj" / "sub" / "facade.py", "from ..engine import compute\n")
    _write(
        tests / "test_via_facade.py",
        "from proj.sub.facade import compute\n\n\ndef test_it() -> None:\n    assert compute() == 1\n",
    )
    _write(tests / "test_engine_direct.py", "from proj.engine import compute\n")
    assert _selected_for(tmp_path, src, tests, "src/proj/engine.py") == {
        "tests/unit/test_via_facade.py",
        "tests/unit/test_engine_direct.py",
    }


# ---------------------------------------------------------------------------
# _resolve_relative_import: direct unit coverage (review follow-up, #5111)
# ---------------------------------------------------------------------------
#
# The fixtures above exercise this function only indirectly, through three
# end-to-end selection tests that each build a tree, run the analyser, and
# compare file sets -- the right *integration* coverage, but an expensive way
# to pin pure arithmetic. A reviewer's manual trace caught a `>` that should
# have been `>=`: when a relative import climbs exactly as many levels as the
# current module has parent segments (not just past them), the old check let
# it through and returned a fabricated top-level module name instead of
# `None`. The same off-by-one made every relative import in a bare top-level
# module (no enclosing package at all) resolve to a fabricated name too,
# since an empty `package_parts` never satisfied the strict `>`.


def test_level_one_resolves_against_the_full_package() -> None:
    assert _resolve_relative_import(current_module="a.b.c", is_package_init=False, level=1, module="d") == "a.b.d"


def test_level_two_climbs_one_parent() -> None:
    assert _resolve_relative_import(current_module="a.b.c", is_package_init=False, level=2, module="e") == "a.e"


def test_a_packages_own_init_is_its_own_package_for_level_one() -> None:
    """``a/b/__init__.py``'s own package is ``a.b``, not ``a`` -- the case almost everyone gets wrong first time."""
    assert _resolve_relative_import(current_module="a.b", is_package_init=True, level=1, module="c") == "a.b.c"


def test_climbing_exactly_to_the_top_returns_none_not_a_fabricated_name() -> None:
    """The off-by-one: ``strip == len(package_parts)`` must refuse, not silently succeed.

    ``a/b/c.py``'s package is ``a.b`` (two segments). A level-3 import strips
    two segments, landing exactly on nothing left to resolve against --
    Python raises ``ImportError`` here, so this must not return a bare
    ``"d"`` as if ``d`` were a real top-level module.
    """
    assert _resolve_relative_import(current_module="a.b.c", is_package_init=False, level=3, module="d") is None


def test_climbing_past_the_top_returns_none() -> None:
    assert _resolve_relative_import(current_module="a.b.c", is_package_init=False, level=4, module="d") is None


def test_a_bare_top_level_module_has_no_package_to_resolve_against() -> None:
    """``widget.py`` at the source root: even ``level=1`` has nothing to climb from.

    Regression case for the same off-by-one: ``package_parts`` is already
    empty here (a non-init file strips its own name, leaving nothing), so
    the boundary check has to fire at ``strip == 0`` too, not only for a
    deeper climb.
    """
    assert _resolve_relative_import(current_module="widget", is_package_init=False, level=1, module="sibling") is None


def test_bare_from_dot_import_with_no_named_module() -> None:
    """``from . import x`` -- ``module`` is ``None`` on the AST node, not empty string."""
    assert _resolve_relative_import(current_module="a.b.c", is_package_init=False, level=1, module=None) == "a.b"
