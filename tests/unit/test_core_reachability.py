"""No module under ``src/bernstein/core`` may sit in the tree with no importer.

#4526 traced ~150 candidates in one pass and the per-package audits closed the
ones that existed then. Nothing stopped the next one: a module with real code, a
green unit suite and no caller reads as a working feature, so contributors extend
it, reviewers trust it, and CI pays for a suite that protects behaviour nobody
can invoke. The audits are a sweep; this is the invariant, so the next orphan
fails the pull request that creates it.

**Reachability is computed the way the package resolves imports, not by grep.**

* Legacy paths such as ``bernstein.core.token_monitor`` are served by
  ``_CoreRedirectFinder`` in ``bernstein/core/__init__.py``. A scan that only
  looks for ``bernstein.core.tokens.token_monitor`` calls the module an orphan
  while the orchestrator imports it on every run, so the alias table is applied
  before a name is judged - and ``test_the_alias_table_makes_a_legacy_import_reachable``
  fails if that resolution is ever dropped.
* Compat redirects are excluded from the scan. Naming a module in a redirect is
  what makes the legacy path work; it is not evidence that anyone calls it, and a
  shim that re-exports a dead module would otherwise vouch for it forever. The
  alias table is passed as the excluded file today - it declares its redirects as
  strings, so the AST does not read them as imports either, and the exclusion is
  what keeps that true if the table is ever rewritten as real imports.
* Having an importer is not the same as being reachable. Two dead modules that
  import each other each have one, and a scan that stops at "somebody imports it"
  reports both as live. Reachability is seeded from callers outside the module's
  own package and then closed over intra-package edges.
* A package ``__init__.py`` is a node in that graph rather than a caller in its
  own right. ``notifications/sinks/__init__.py`` re-exports six drivers; the
  re-export makes them reachable only once something outside the package imports
  the package, which is what actually happens at runtime.

**The allowlist.** ``core_reachability_allowlist.txt`` holds every module the
static trace cannot reach today, one per line, each with a reason. It is a data
file rather than a block in this module because it has 300-odd entries and
shrinks one package at a time: a data file keeps this test readable and makes
each removal a one-line diff. Two kinds of entry exist today - a module reached
by a mechanism the static trace cannot see (the reason names the mechanism), and
a module whose wire-or-delete decision is still open, either a #4526 candidate
awaiting its package audit or one that landed under ``core/`` while #4526 was
open (the reason says which). The baseline is the tree this guard ships against,
not the one the file was first written against. The list
may only ever SHRINK: ``test_the_allowlist_has_no_stale_entries`` fails when an
entry becomes reachable or leaves the tree, so wiring or deleting a module forces
its line out rather than letting the list rot into a permanent exemption.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
CORE_DIR = SRC_ROOT / "bernstein" / "core"

#: The redirect declaration, not a call site: every module it names would read as
#: reachable if this file were scanned.
ALIAS_TABLE = CORE_DIR / "__init__.py"

ALLOWLIST_FILE = Path(__file__).with_name("core_reachability_allowlist.txt")

pytestmark = pytest.mark.skipif(
    not CORE_DIR.is_dir(),
    reason="reachability invariant only runs inside a bernstein source checkout",
)


def _module_name(path: Path, src_root: Path) -> str:
    """Dotted name of ``path``; a package ``__init__.py`` names its package."""
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(tree: ast.Module, own_package: str) -> set[str]:
    """Every dotted name ``tree`` imports, relative imports resolved.

    ``from x import y`` is ambiguous between an attribute of ``x`` and the
    submodule ``x.y``, so both candidates are emitted; only the ones that match a
    real module survive the caller's lookup.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = own_package.split(".") if own_package else []
                trimmed = base[: len(base) - node.level + 1]
                prefix = ".".join([*trimmed, node.module] if node.module else trimmed)
            else:
                prefix = node.module or ""
            names.add(prefix)
            names.update(f"{prefix}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _own_package(path: Path, src_root: Path) -> str:
    """Dotted package a module lives in, for resolving its relative imports."""
    if path.name == "__init__.py":
        return _module_name(path, src_root)
    return ".".join(path.relative_to(src_root).with_suffix("").parts[:-1])


@cache
def _file_imports(path: Path, src_root: Path) -> frozenset[str]:
    """Imports of one file, parsed once per session.

    Three passes over ``src`` run in this module and each parses ~2000 files; the
    uncached shape spent most of a minute of CI wall time re-parsing the same tree.
    Names are cached rather than syntax trees, which keeps the cache small.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return frozenset()
    return frozenset(_imported_names(tree, _own_package(path, src_root)))


def unreachable_modules(
    src_root: Path,
    core_dir: Path,
    *,
    redirects: Mapping[str, str] | None = None,
    excluded: Iterable[Path] = (),
) -> set[str]:
    """Dotted names of modules under ``core_dir`` no importer outside them reaches.

    Package ``__init__.py`` files take part in the graph but are never judged: a
    package with no submodules of its own is not an orphan, it is a namespace.
    """
    skip = {p.resolve() for p in excluded}
    nodes = {
        _module_name(path, src_root): path for path in sorted(core_dir.rglob("*.py")) if path.resolve() not in skip
    }
    judged = {name for name, path in nodes.items() if path.name != "__init__.py"}

    alias = {
        (legacy if legacy.startswith("bernstein.") else f"bernstein.core.{legacy}"): real
        for legacy, real in (redirects or {}).items()
    }

    importers: dict[str, set[Path]] = defaultdict(set)
    for path in sorted(src_root.rglob("*.py")):
        if path.resolve() in skip:
            continue
        for name in _file_imports(path, src_root):
            target = alias.get(name, name)
            if target in nodes and nodes[target] != path:
                importers[target].add(path)

    packages: dict[Path, list[str]] = defaultdict(list)
    for name, path in nodes.items():
        packages[path.parent].append(name)

    unreachable: set[str] = set()
    for package_dir, names in packages.items():
        reachable = {name for name in names if any(p.parent != package_dir for p in importers.get(name, ()))}
        grew = True
        while grew:
            grew = False
            for name in names:
                if name in reachable:
                    continue
                inside = (p for p in importers.get(name, ()) if p.parent == package_dir)
                if any(_module_name(p, src_root) in reachable for p in inside):
                    reachable.add(name)
                    grew = True
        unreachable |= {name for name in names if name in judged and name not in reachable}
    return unreachable


def _read_allowlist(path: Path = ALLOWLIST_FILE) -> dict[str, str]:
    """``{dotted module: reason}``; ``#`` lines and blanks are prose."""
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        module, _, reason = stripped.partition(":")
        entries[module.strip()] = reason.strip()
    return entries


@lru_cache(maxsize=1)
def _current_unreachable() -> frozenset[str]:
    """One pass over ``src`` for the whole module; the naive shape re-parsed per test."""
    from bernstein.core import _REDIRECT_MAP

    return frozenset(unreachable_modules(SRC_ROOT, CORE_DIR, redirects=_REDIRECT_MAP, excluded=[ALIAS_TABLE]))


def _first(names: Iterable[str], limit: int = 15) -> str:
    """A failure naming 300 modules is unreadable; name enough to start on."""
    listed = sorted(names)
    head = ", ".join(listed[:limit])
    return head if len(listed) <= limit else f"{head} (+{len(listed) - limit} more)"


def _write_tree(root: Path, files: Mapping[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_no_new_unreachable_module_under_core() -> None:
    """A module nothing can import must fail its own pull request."""
    appeared = _current_unreachable() - _read_allowlist().keys()
    assert not appeared, (
        f"{len(appeared)} module(s) under src/bernstein/core that no importer reaches: "
        f"{_first(appeared)}. Wire each one "
        "to a consumer that exists today, or delete the module together with its tests and its "
        "bernstein/core/__init__.py alias entry. If a mechanism the static trace cannot see does "
        f"reach it, add a line to {ALLOWLIST_FILE.name} naming that mechanism."
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """The list may only shrink, so wiring or deleting a module forces its line out."""
    stale = _read_allowlist().keys() - _current_unreachable()
    assert not stale, (
        f"these are reachable now or gone from the tree: {_first(stale)}. Strike them from "
        f"{ALLOWLIST_FILE.name} - an exemption that outlives its reason is how the list stops "
        "meaning anything."
    )


def test_every_allowlist_entry_names_a_mechanism() -> None:
    """An entry with no reason is a suppression, not an allowlist entry."""
    unreasoned = {module for module, reason in _read_allowlist().items() if not reason}
    assert not unreasoned, (
        f"allowlist entries with no reason: {_first(unreasoned)}. Every line must say what reaches the "
        "module - a dynamic loader, an entry point, the lazy-alias finder - or that the module is "
        "still waiting on its package audit."
    )


def test_the_allowlist_file_is_sorted_and_unique() -> None:
    """Out of order or duplicated, the file stops reading as one line per decision."""
    modules = [
        line.split(":", 1)[0].strip()
        for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    duplicated = sorted({name for name in modules if modules.count(name) > 1})
    assert not duplicated, f"duplicated allowlist entries: {duplicated}"
    assert modules == sorted(modules), (
        f"{ALLOWLIST_FILE.name} is not sorted; keep it sorted so removing an entry is a one-line diff."
    )


# ---------------------------------------------------------------------------
# The trace itself, on fixture trees: without these the invariant passes vacuously
# ---------------------------------------------------------------------------


def test_an_unreachable_module_in_a_fixture_tree_is_reported(tmp_path: Path) -> None:
    """The load-bearing case: a module with no importer must be found."""
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/wired.py": "VALUE = 1\n",
            "pkg/core/orphan.py": "VALUE = 2\n",
            "pkg/app.py": "from pkg.core.wired import VALUE\n",
        },
    )
    assert unreachable_modules(tmp_path, tmp_path / "pkg" / "core") == {"pkg.core.orphan"}


def test_an_allowlisted_module_is_not_reported_as_new(tmp_path: Path) -> None:
    """Same tree, same orphan, one allowlist line: the invariant passes."""
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/orphan.py": "VALUE = 2\n",
        },
    )
    allowlist_file = tmp_path / "allowlist.txt"
    allowlist_file.write_text(
        "# one line per module, each with a reason\npkg.core.orphan: loaded by name from the driver table\n",
        encoding="utf-8",
    )
    found = unreachable_modules(tmp_path, tmp_path / "pkg" / "core")
    allowlist = _read_allowlist(allowlist_file)

    assert found == {"pkg.core.orphan"}
    assert not found - allowlist.keys()
    assert allowlist["pkg.core.orphan"]


def test_a_mutually_importing_dead_cluster_is_still_unreachable(tmp_path: Path) -> None:
    """Two dead modules must not vouch for each other.

    This is the failure the "does anything import it?" shape cannot see, and it is
    how a whole dead corner of a package survives a cleanup.
    """
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/dead_a.py": "from pkg.core.dead_b import VALUE\n",
            "pkg/core/dead_b.py": "from pkg.core.dead_a import VALUE\nVALUE = 1\n",
            "pkg/core/wired.py": "from pkg.core.helper import VALUE\n",
            "pkg/core/helper.py": "VALUE = 1\n",
            "pkg/app.py": "from pkg.core.wired import VALUE\n",
        },
    )
    found = unreachable_modules(tmp_path, tmp_path / "pkg" / "core")
    assert found == {"pkg.core.dead_a", "pkg.core.dead_b"}


def test_a_relative_import_counts_as_a_caller(tmp_path: Path) -> None:
    """``from .helper import x`` is a call site; missing it invents orphans."""
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/wired.py": "from .helper import VALUE\n",
            "pkg/core/helper.py": "VALUE = 1\n",
            "pkg/app.py": "from pkg.core.wired import VALUE\n",
        },
    )
    assert unreachable_modules(tmp_path, tmp_path / "pkg" / "core") == set()


# ---------------------------------------------------------------------------
# Alias-table awareness and its exclusion, pinned against the real tree
# ---------------------------------------------------------------------------


def test_the_alias_table_makes_a_legacy_import_reachable() -> None:
    """``token_monitor`` is imported only as ``bernstein.core.token_monitor``.

    Without redirect resolution the trace calls it an orphan, which is how a
    detector ends up demanding the deletion of a module the orchestrator imports
    on every run (#4525).
    """
    from bernstein.core import _REDIRECT_MAP

    module = "bernstein.core.tokens.token_monitor"
    assert (CORE_DIR / "tokens" / "token_monitor.py").is_file()
    assert module not in _current_unreachable()

    without_aliases = unreachable_modules(SRC_ROOT, CORE_DIR, excluded=[ALIAS_TABLE])
    assert module in without_aliases, (
        "token_monitor is reachable by a canonical import now, so it no longer pins alias "
        "resolution; pick another alias-only module for this test rather than deleting it."
    )
    assert _REDIRECT_MAP["token_monitor"] == module


def test_a_compat_redirect_does_not_count_as_a_caller(tmp_path: Path) -> None:
    """A shim that re-exports a dead module must not keep it alive.

    Without the exclusion every module named in a redirect reads as reachable and
    the trace reports nothing at all.
    """
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/orphan.py": "VALUE = 2\n",
            "pkg/compat.py": "from pkg.core.orphan import VALUE as VALUE\n",
        },
    )
    core = tmp_path / "pkg" / "core"
    shim = tmp_path / "pkg" / "compat.py"

    assert unreachable_modules(tmp_path, core) == set()
    assert unreachable_modules(tmp_path, core, excluded=[shim]) == {"pkg.core.orphan"}
