"""Reachability scan for a subpackage of ``src/bernstein``.

A module with a green unit suite and no runtime caller reads as a working
feature: a contributor extends it, a reviewer trusts it, and CI pays for a
suite that protects nothing. The scan here is what lets a per-subpackage
guard fail CI on the next such module.

Reachability has to be computed the way the package actually resolves
imports. Legacy paths such as ``bernstein.core.token_monitor`` are served by
``_CoreRedirectFinder`` in ``bernstein/core/__init__.py``, so a scan that
only looks for the canonical ``bernstein.core.<sub>.<name>`` reports live
modules as orphans. The alias table itself is excluded from the scan: naming
a module there is what makes the legacy path work, not evidence that anyone
calls it.

Having an importer is also not the same as being reachable. Two dead modules
that import each other each have one, so the scan seeds from callers outside
the subpackage and closes over intra-package edges.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from bernstein.core import _REDIRECT_MAP

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

# The alias table is a redirect declaration, not a call site.
EXCLUDED_FROM_SCAN = frozenset({SRC_ROOT / "bernstein" / "core" / "__init__.py"})


@lru_cache(maxsize=1)
def import_index() -> tuple[dict[str, tuple[Path, ...]], dict[tuple[str, str], tuple[Path, ...]]]:
    """One AST pass over ``src`` instead of one per module.

    Built once per test session and shared by every subpackage scan; the
    naive shape re-parsed the whole tree for each scanned module and cost
    over a minute of CI wall time.
    """
    dotted: dict[str, list[Path]] = {}
    from_package: dict[tuple[str, str], list[Path]] = {}

    for path in SRC_ROOT.rglob("*.py"):
        if path in EXCLUDED_FROM_SCAN:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = node.module or ""
                dotted.setdefault(source, []).append(path)
                for alias in node.names:
                    from_package.setdefault((source, alias.name), []).append(path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    dotted.setdefault(alias.name, []).append(path)

    return (
        {key: tuple(value) for key, value in dotted.items()},
        {key: tuple(value) for key, value in from_package.items()},
    )


@dataclass(frozen=True, slots=True)
class SubpackageScan:
    """Orphan scan over the top-level modules of one subpackage.

    ``package`` is the dotted name the directory is imported as; both are
    passed explicitly rather than derived so a caller cannot silently scan a
    directory that no longer matches the package it names.
    """

    directory: Path
    package: str

    def module_names(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.py") if p.name != "__init__.py")

    def _is_inside(self, path: Path) -> bool:
        """Whether ``path`` is one of the modules this scan reasons about."""
        return path.parent == self.directory

    def import_targets(self, module: str) -> set[str]:
        """Every dotted path that resolves to ``<directory>/<module>.py``."""
        canonical = f"{self.package}.{module}"
        targets = {canonical}
        for legacy, real in _REDIRECT_MAP.items():
            if real == canonical:
                targets.add(legacy if legacy.startswith("bernstein.") else f"bernstein.core.{legacy}")
        return targets

    def importers_of(self, module: str) -> set[Path]:
        """Every file that imports ``module``, by any of its resolvable paths."""
        targets = self.import_targets(module)
        packages = {t.rsplit(".", 1)[0] for t in targets}
        own_file = self.directory / f"{module}.py"
        dotted, from_package = import_index()

        found: set[Path] = set()
        for target in targets:
            found |= {p for p in dotted.get(target, ()) if p != own_file}
        for package in packages:
            found |= {p for p in from_package.get((package, module), ()) if p != own_file}
        return found

    def importer_of(self, module: str) -> Path | None:
        """Any one importer of ``module``, or ``None`` when it has none."""
        return next(iter(sorted(self.importers_of(module))), None)

    def reachable_modules(self, importers: dict[str, set[Path]]) -> set[str]:
        """Modules reachable from a caller outside the subpackage.

        Seeded from callers outside the directory and closed over
        intra-package edges, so a mutually importing dead cluster stays
        unreachable instead of vouching for itself.
        """
        reachable = {name for name, paths in importers.items() if any(not self._is_inside(p) for p in paths)}
        grew = True
        while grew:
            grew = False
            for name, paths in importers.items():
                if name in reachable:
                    continue
                if any(self._is_inside(p) and p.stem in reachable for p in paths):
                    reachable.add(name)
                    grew = True
        return reachable

    def orphans(self) -> set[str]:
        importers = {name: self.importers_of(name) for name in self.module_names()}
        return set(importers) - self.reachable_modules(importers)


def assert_orphans_match(scan: SubpackageScan, known: frozenset[str], label: str) -> None:
    """The set of caller-less modules may shrink, never grow."""
    current = scan.orphans()

    appeared = sorted(current - known)
    assert not appeared, (
        f"new caller-less modules under {label}: {appeared}. Wire each one to a "
        "consumer that exists today, or delete the module together with its tests and "
        "its bernstein/core/__init__.py alias entry."
    )

    removed = sorted(known - current)
    assert not removed, (
        f"{removed} now has a caller or is gone from the tree; strike it from "
        "KNOWN_ORPHANS so the list keeps shrinking."
    )
