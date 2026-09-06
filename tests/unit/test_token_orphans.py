"""No module under ``core/tokens/`` may sit in the tree with no caller.

A module with a green unit suite and no runtime caller reads as a working
feature: a contributor extends it, a reviewer trusts it, and CI pays for a
suite that protects nothing. This test makes the next such module fail CI
instead of joining the pile.

Reachability has to be computed the way the package actually resolves
imports. Legacy paths such as ``bernstein.core.token_monitor`` are served by
``_CoreRedirectFinder`` in ``bernstein/core/__init__.py``, so a scan that
only looks for ``bernstein.core.tokens.<name>`` reports live modules as
orphans - ``token_monitor`` is imported by the orchestrator through exactly
that legacy path. The alias table itself is excluded from the scan: naming
a module there is what makes the legacy path work, not evidence that anyone
calls it.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from bernstein.core import _REDIRECT_MAP
from tests.unit._ratchet import assert_ratchet_matches

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS_DIR = REPO_ROOT / "src" / "bernstein" / "core" / "tokens"
TOKENS_PKG = "bernstein.core.tokens"

# The alias table is a redirect declaration, not a call site.
EXCLUDED_FROM_SCAN = {REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py"}

# Modules known to have no caller today, kept as an exact set rather than a
# floor: a new orphan fails this test, and removing one of these fails it too
# until the name is struck from the list. The list only ever shrinks.
KNOWN_ORPHANS = frozenset(
    {
        "cache_token_tracker",
        "claude_prompt_cache_optimizer",
        "context_fallback",
        "context_inheritance",
        "image_optimizer",
        "prompt_injection",
        "token_binding",
        "token_breakdown",
        "token_counter",
    }
)


def _module_names() -> list[str]:
    return sorted(p.stem for p in TOKENS_DIR.glob("*.py") if p.name != "__init__.py")


def _import_targets(module: str) -> set[str]:
    """Every dotted path that resolves to ``core/tokens/<module>.py``."""
    canonical = f"{TOKENS_PKG}.{module}"
    targets = {canonical}
    for legacy, real in _REDIRECT_MAP.items():
        if real == canonical:
            targets.add(legacy if legacy.startswith("bernstein.") else f"bernstein.core.{legacy}")
    return targets


def _scanned_files() -> list[Path]:
    return [p for p in (REPO_ROOT / "src").rglob("*.py") if p not in EXCLUDED_FROM_SCAN]


@lru_cache(maxsize=1)
def _import_index() -> tuple[dict[str, list[Path]], dict[tuple[str, str], list[Path]]]:
    """One AST pass over ``src`` instead of one per module.

    Rebuilt per test session; the naive shape re-parsed the whole tree for
    each of the 30-odd modules and cost over a minute of CI wall time.
    """
    dotted: dict[str, list[Path]] = {}
    from_package: dict[tuple[str, str], list[Path]] = {}

    for path in _scanned_files():
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

    return dotted, from_package


def _importer_of(module: str) -> Path | None:
    targets = _import_targets(module)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = TOKENS_DIR / f"{module}.py"
    dotted, from_package = _import_index()

    for target in targets:
        for path in dotted.get(target, ()):
            if path != own_file:
                return path
    for package in packages:
        for path in from_package.get((package, module), ()):
            if path != own_file:
                return path
    return None


def _importers_of(module: str) -> set[Path]:
    """Every file that imports `module`, by any of its resolvable paths."""
    targets = _import_targets(module)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = TOKENS_DIR / f"{module}.py"
    dotted, from_package = _import_index()

    found: set[Path] = set()
    for target in targets:
        found |= {p for p in dotted.get(target, ()) if p != own_file}
    for package in packages:
        found |= {p for p in from_package.get((package, module), ()) if p != own_file}
    return found


def reachable_modules(importers: dict[str, set[Path]]) -> set[str]:
    """Modules reachable from a caller outside ``core/tokens/``.

    Having an importer is not the same as being reachable: two dead modules
    that import each other each have one, and a scan that stops at "somebody
    imports it" reports both as live. Seed from callers outside the package
    and close over intra-package edges instead.
    """
    reachable = {name for name, paths in importers.items() if any(TOKENS_DIR not in path.parents for path in paths)}
    grew = True
    while grew:
        grew = False
        for name, paths in importers.items():
            if name in reachable:
                continue
            if any(p.parent == TOKENS_DIR and p.stem in reachable for p in paths):
                reachable.add(name)
                grew = True
    return reachable


def _current_orphans() -> set[str]:
    importers = {name: _importers_of(name) for name in _module_names()}
    return set(importers) - reachable_modules(importers)


def test_no_new_orphan_token_modules() -> None:
    """The set of caller-less modules may shrink, never grow (#5552, #5503)."""
    current = _current_orphans()
    file_mapping = {name: f"src/bernstein/core/tokens/{name}.py" for name in _module_names()}
    assert_ratchet_matches(
        current,
        KNOWN_ORPHANS,
        subject="core/tokens/ orphan allowlist",
        constant_name="KNOWN_ORPHANS",
        file_mapping=file_mapping,
        wire_hint="Wire each one to a consumer that exists today, or delete the module together with its tests and its bernstein/core/__init__.py alias entry.",
    )


def test_no_stale_exemptions() -> None:
    """The exemption list may only shrink (asserted in test_no_new_orphan_token_modules)."""
    test_no_new_orphan_token_modules()


def test_a_wired_module_is_seen_through_its_legacy_alias() -> None:
    """`token_monitor` is reachable only via `bernstein.core.token_monitor`.

    Without redirect resolution the scan calls it an orphan, which is how a
    detector ends up demanding the deletion of a module the orchestrator
    imports on every run.
    """
    assert "token_monitor" in _module_names()
    assert _importer_of("token_monitor") is not None


def test_the_alias_table_alone_does_not_count_as_a_caller() -> None:
    """Otherwise every module listed in the redirect map reads as reachable."""
    assert KNOWN_ORPHANS, "the guard needs at least one known orphan to be meaningful"
    orphan = sorted(KNOWN_ORPHANS)[0]
    alias_table = (REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{orphan}"' in alias_table, f"{orphan} is expected to be listed in the alias table"
    assert _importer_of(orphan) is None


def test_a_mutually_importing_dead_cluster_is_still_orphaned() -> None:
    """Two dead modules that import each other must not vouch for each other.

    This is the failure the "does anything import it?" shape cannot see, and
    it is how a whole dead corner of a package survives a cleanup.
    """
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "dead_a": {TOKENS_DIR / "dead_b.py"},
        "dead_b": {TOKENS_DIR / "dead_a.py"},
    }
    assert reachable_modules(importers) == {"wired"}


def test_a_module_used_only_by_a_wired_module_is_reachable() -> None:
    """Positive control: intra-package edges do carry reachability."""
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "helper": {TOKENS_DIR / "wired.py"},
    }
    assert reachable_modules(importers) == {"wired", "helper"}
