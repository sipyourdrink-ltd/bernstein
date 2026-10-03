#!/usr/bin/env python
"""Gate for issue #5053: a security control that ships with no production caller.

``vulture`` asks whether a symbol is dead. This asks a different question:
is the symbol *reached from the shipped package*? A control whose only
importers are its own tests, an ``__init__`` re-export, or the lazy module
map in ``src/bernstein/core/__init__.py`` reads as live to every existing
tool - its tests pass, its name greps - while never running in production.

What counts as a reference
--------------------------
Only ``Name`` loads and attribute accesses inside ``src/bernstein/`` count.
Deliberately *not* references:

* ``import`` / ``from ... import`` statements - a re-export is an import, so
  ``__init__`` re-exports contribute nothing on their own;
* string literals - which is why ``__all__`` entries and the lazy module map
  in ``core/__init__.py`` (a ``dict[str, str]`` of module paths) do not keep
  a symbol alive;
* anything under ``tests/`` - the reference scan never leaves the package.

Reachability is a fixpoint, not a single pass. A reference only counts when
it is made from module-level code, from a symbol that is itself reachable,
or from outside the scanned packages. So a symbol called only by another
unreachable symbol stays unreachable, and allowlisting a symbol does *not*
make what it calls reachable.

Static approximation
--------------------
Unqualified names and method attributes are matched by name, and a type
annotation counts as a reference. These cases can *under*-report unused
symbols. Module-qualified references to top-level symbols are matched to
their imported module instead: an unrelated object's ``.create_context`` must
not make ``security.policy.create_context`` appear live. Dynamic bindings
through arbitrary objects cannot be resolved statically; document any such
production caller when reviewing a finding.

The allowlist
-------------
``unreachable_controls_allowlist.txt`` carries one entry per known-unreached
symbol, each with a written reason. A reason is mandatory: an entry with an
empty reason, or one still carrying the ``REASON REQUIRED`` marker that
``--update`` writes for a new finding, fails the gate. An entry whose symbol
is now reachable (or gone) also fails, so the list cannot rot.

Run locally::

    uv run python scripts/check_unreachable_controls.py
    uv run python scripts/check_unreachable_controls.py --update

Exit codes:
  0 = every unreachable public symbol is allowlisted with a reason.
  1 = an unlisted finding, or a malformed / reasonless / stale allowlist entry.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SCAN_ROOTS = (
    Path("src/bernstein/core/security"),
    Path("src/bernstein/core/identity"),
)
DEFAULT_REFERENCE_ROOT = Path("src/bernstein")
DEFAULT_ALLOWLIST = Path("unreachable_controls_allowlist.txt")

#: Marker ``--update`` writes for a newly discovered symbol. The gate refuses
#: it, so a new finding cannot be silenced without someone writing a reason.
REASON_REQUIRED = "REASON REQUIRED"

_HEADER = """\
# Allowlist for scripts/check_unreachable_controls.py (issue #5053).
#
# Each line names a public symbol under src/bernstein/core/security/ or
# src/bernstein/core/identity/ that no production code path reaches, and
# states why. The reason after '#' is mandatory - an entry without one, or
# one still carrying the 'REASON REQUIRED' marker, fails the gate.
#
# Format:
#   <path>::<function>            # <reason>
#   <path>::<Class>.<method>      # <reason>
#
# Regenerate the entry list (reasons for existing entries are preserved):
#   uv run python scripts/check_unreachable_controls.py --update
#
# Removing an entry is the goal: wire the symbol into a live code path, or
# delete it. An entry whose symbol became reachable fails the gate too, so a
# stale line cannot sit here unnoticed.
"""


@dataclass(frozen=True, order=True)
class SymbolId:
    """Identity of a tracked definition: file, top-level name, optional member."""

    path: str
    owner: str
    member: str | None = None

    @property
    def local_name(self) -> str:
        """The name a caller would have to write to reach this definition."""
        return self.member if self.member is not None else self.owner

    @property
    def is_public(self) -> bool:
        """True when neither the owner nor the member is underscore-prefixed."""
        if self.owner.startswith("_"):
            return False
        return self.member is None or not self.member.startswith("_")

    @property
    def key(self) -> str:
        """Stable ``path::name`` form used in the allowlist file."""
        qualified = self.owner if self.member is None else f"{self.owner}.{self.member}"
        return f"{self.path}::{qualified}"


@dataclass(frozen=True)
class Finding:
    """An unreachable public symbol, with the source location to report."""

    symbol: SymbolId
    lineno: int


@dataclass
class References:
    """Names, method attributes, and resolved top-level module attributes."""

    names: set[str] = field(default_factory=set)
    attributes: set[str] = field(default_factory=set)
    qualified: set[SymbolId] = field(default_factory=set)

    def update(self, other: References) -> None:
        self.names.update(other.names)
        self.attributes.update(other.attributes)
        self.qualified.update(other.qualified)


_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return None


def collect_symbols(scan_roots: list[Path], repo_root: Path) -> tuple[dict[SymbolId, int], set[str]]:
    """Return ``(symbol -> lineno, scanned relative paths)`` for the scan roots.

    Private definitions are tracked too. They are never reported, but they must
    participate in the fixpoint: a dead private helper must not keep the public
    symbols it calls alive.

    ``__init__.py`` is skipped - a re-export defines nothing of its own.
    """
    symbols: dict[SymbolId, int] = {}
    scanned: set[str] = set()
    for root in scan_roots:
        for path in _iter_python_files(root):
            if path.name == "__init__.py":
                continue
            tree = _parse(path)
            if tree is None:
                continue
            rel = path.relative_to(repo_root).as_posix()
            scanned.add(rel)
            for node in tree.body:
                if not isinstance(node, _DEF_NODES):
                    continue
                symbols[SymbolId(rel, node.name)] = node.lineno
                if not isinstance(node, ast.ClassDef):
                    continue
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols[SymbolId(rel, node.name, member.name)] = member.lineno
    return symbols, scanned


def _module_name(path: str) -> str:
    """Return the import path of a tracked ``src/`` Python module."""
    return path.removeprefix("src/").removesuffix(".py").replace("/", ".")


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _import_bindings(
    tree: ast.Module,
    module: str,
    modules: dict[str, str],
    symbols: dict[SymbolId, int],
) -> tuple[dict[str, str], dict[str, SymbolId]]:
    """Resolve imported module and symbol aliases used by attribute/name loads."""
    module_aliases: dict[str, str] = {}
    symbol_aliases: dict[str, SymbolId] = {}
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            imported_module = "." * node.level + (node.module or "")
            if node.level:
                try:
                    imported_module = importlib.util.resolve_name(imported_module, package)
                except ImportError:
                    continue
            for alias in node.names:
                local_name = alias.asname or alias.name
                candidate_module = f"{imported_module}.{alias.name}"
                if candidate_module in modules:
                    module_aliases[local_name] = candidate_module
                elif imported_module in modules:
                    candidate = SymbolId(modules[imported_module], alias.name)
                    if candidate in symbols:
                        symbol_aliases[local_name] = candidate
    return module_aliases, symbol_aliases


def _record(
    refs: References,
    node: ast.AST,
    module_aliases: dict[str, str],
    symbol_aliases: dict[str, SymbolId],
    modules: dict[str, str],
) -> None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        refs.names.add(node.id)
        if node.id in symbol_aliases:
            refs.qualified.add(symbol_aliases[node.id])
    elif isinstance(node, ast.Attribute):
        refs.attributes.add(node.attr)
        receiver = _dotted_name(node.value)
        if receiver is None:
            return
        first, _, rest = receiver.partition(".")
        imported = module_aliases.get(first)
        if imported is None:
            return
        module = f"{imported}.{rest}" if rest else imported
        if module in modules:
            refs.qualified.add(SymbolId(modules[module], node.attr))


def collect_references(
    reference_root: Path,
    repo_root: Path,
    symbols: dict[SymbolId, int],
    scanned: set[str],
) -> tuple[References, dict[SymbolId, References]]:
    """Split every name reference in the package by the definition that owns it.

    Returns ``(root_refs, owned_refs)``. ``root_refs`` are references made from
    module-level code or from a file outside the scanned packages: those are
    unconditionally live. ``owned_refs`` maps a tracked definition to the names
    its body mentions, and only counts once that definition is reachable.
    """
    root_refs = References()
    owned_refs: dict[SymbolId, References] = defaultdict(References)
    modules = {_module_name(symbol.path): symbol.path for symbol in symbols}

    def walk(
        node: ast.AST,
        owner: SymbolId | None,
        *,
        rel: str,
        tracked: bool,
        at_module_level: bool,
        module_aliases: dict[str, str],
        symbol_aliases: dict[str, SymbolId],
    ) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = owner
            if tracked and isinstance(child, _DEF_NODES):
                if at_module_level and SymbolId(rel, child.name) in symbols:
                    child_owner = SymbolId(rel, child.name)
                elif owner is not None and owner.member is None and SymbolId(rel, owner.owner, child.name) in symbols:
                    child_owner = SymbolId(rel, owner.owner, child.name)
            _record(
                root_refs if owner is None else owned_refs[owner],
                child,
                module_aliases,
                symbol_aliases,
                modules,
            )
            walk(
                child,
                child_owner,
                rel=rel,
                tracked=tracked,
                at_module_level=False,
                module_aliases=module_aliases,
                symbol_aliases=symbol_aliases,
            )

    for path in _iter_python_files(reference_root):
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(repo_root).as_posix()
        module_aliases, symbol_aliases = _import_bindings(tree, _module_name(rel), modules, symbols)
        walk(
            tree,
            None,
            rel=rel,
            tracked=rel in scanned,
            at_module_level=True,
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        )

    return root_refs, owned_refs


def reachable_symbols(
    symbols: dict[SymbolId, int],
    root_refs: References,
    owned_refs: dict[SymbolId, References],
) -> set[SymbolId]:
    """Grow the live-reference set until it stops changing."""
    live = References()
    live.update(root_refs)
    reached: set[SymbolId] = set()
    changed = True
    while changed:
        changed = False
        for symbol in symbols:
            if symbol in reached:
                continue
            if symbol.member is None:
                referenced = symbol.owner in live.names or symbol in live.qualified
            else:
                referenced = symbol.member in live.names or symbol.member in live.attributes
            if not referenced:
                continue
            reached.add(symbol)
            live.update(owned_refs.get(symbol, References()))
            changed = True
    return reached


def find_unreachable(symbols: dict[SymbolId, int], reached: set[SymbolId]) -> list[Finding]:
    """Report unreached public symbols, one line per independent finding.

    A method of an unreachable class is dropped: the class already carries the
    finding, and listing every method under it would bury the signal.
    """
    findings: list[Finding] = []
    for symbol, lineno in symbols.items():
        if symbol in reached or not symbol.is_public:
            continue
        if symbol.member is not None and SymbolId(symbol.path, symbol.owner) not in reached:
            continue
        findings.append(Finding(symbol, lineno))
    return sorted(findings, key=lambda f: (f.symbol.path, f.symbol.owner, f.symbol.member or ""))


def parse_allowlist(path: Path) -> tuple[dict[str, str], list[str]]:
    """Return ``(key -> reason, errors)`` for the allowlist at *path*."""
    reasons: dict[str, str] = {}
    errors: list[str] = []
    if not path.exists():
        return reasons, errors
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry, sep, reason = line.partition("#")
        key = entry.strip()
        reason = reason.strip()
        if "::" not in key:
            errors.append(f"{path}:{lineno}: malformed entry (expected '<path>::<symbol>'): {line}")
            continue
        if key in reasons:
            errors.append(f"{path}:{lineno}: duplicate entry for {key}")
            continue
        if not sep or not reason or reason == REASON_REQUIRED:
            errors.append(f"{path}:{lineno}: {key} has no reason; state why it has no caller")
            continue
        reasons[key] = reason
    return reasons, errors


def render_allowlist(findings: list[Finding], existing: dict[str, str]) -> str:
    """Render the allowlist file, keeping the reason already written for an entry."""
    width = max((len(f.symbol.key) for f in findings), default=0)
    lines = [_HEADER]
    for finding in findings:
        key = finding.symbol.key
        reason = existing.get(key, REASON_REQUIRED)
        lines.append(f"{key.ljust(width)}  # {reason}")
    return "\n".join(lines) + "\n"


def scan(repo_root: Path, scan_roots: list[Path], reference_root: Path) -> list[Finding]:
    """Run the whole analysis against *repo_root* and return the findings."""
    symbols, scanned = collect_symbols(scan_roots, repo_root)
    root_refs, owned_refs = collect_references(reference_root, repo_root, symbols, scanned)
    return find_unreachable(symbols, reachable_symbols(symbols, root_refs, owned_refs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on a public symbol no production code reaches.")
    parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root (default: cwd).")
    parser.add_argument(
        "--scan-roots",
        nargs="*",
        type=Path,
        default=None,
        help="Packages to scan, relative to --repo-root (default: core/security, core/identity).",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        default=None,
        help="Package searched for callers, relative to --repo-root (default: src/bernstein).",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Allowlist file, relative to --repo-root (default: unreachable_controls_allowlist.txt).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite the allowlist from the current tree, preserving written reasons.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    scan_roots = [repo_root / p for p in (args.scan_roots or list(DEFAULT_SCAN_ROOTS))]
    reference_root = repo_root / (args.reference_root or DEFAULT_REFERENCE_ROOT)
    allowlist_path = repo_root / (args.allowlist or DEFAULT_ALLOWLIST)

    missing = [p for p in [*scan_roots, reference_root] if not p.is_dir()]
    if missing:
        for path in missing:
            print(f"check_unreachable_controls: no such directory: {path}", file=sys.stderr)
        return 1

    findings = scan(repo_root, scan_roots, reference_root)
    reasons, errors = parse_allowlist(allowlist_path)

    if args.update:
        allowlist_path.write_text(render_allowlist(findings, reasons), encoding="utf-8")
        print(f"check_unreachable_controls: wrote {len(findings)} entries to {allowlist_path}")
        return 0

    found_keys = {f.symbol.key for f in findings}
    unlisted = [f for f in findings if f.symbol.key not in reasons]
    stale = sorted(key for key in reasons if key not in found_keys)

    if not unlisted and not stale and not errors:
        print(f"check_unreachable_controls: OK ({len(findings)} known-unreached symbols, all with a reason)")
        return 0

    if unlisted:
        print(
            "check_unreachable_controls: public symbols with no production caller and no allowlist entry:",
            file=sys.stderr,
        )
        for finding in unlisted:
            print(f"  {finding.symbol.path}:{finding.lineno}: {finding.symbol.key}", file=sys.stderr)
    if stale:
        print(
            "\ncheck_unreachable_controls: allowlist entries that are now reachable or gone:",
            file=sys.stderr,
        )
        for key in stale:
            print(f"  {key}", file=sys.stderr)
    for error in errors:
        print(f"check_unreachable_controls: {error}", file=sys.stderr)

    print(
        "\nEither wire the symbol into a live code path (preferred), delete it, or\n"
        f"add it to {DEFAULT_ALLOWLIST} with a written reason. Regenerate the entry\n"
        "list with:\n"
        "  uv run python scripts/check_unreachable_controls.py --update\n"
        "and replace every 'REASON REQUIRED' marker before committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
