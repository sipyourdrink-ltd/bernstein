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
from tests.unit._orphan_scan import describe_ratchet_drift, resolve_branch_only_ref, scan_at_ref

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


def _tokens_dir_under(root: Path) -> Path:
    return root / "src" / "bernstein" / "core" / "tokens"


def _module_names(root: Path = REPO_ROOT) -> list[str]:
    return sorted(p.stem for p in _tokens_dir_under(root).glob("*.py") if p.name != "__init__.py")


def _import_targets(module: str) -> set[str]:
    """Every dotted path that resolves to ``core/tokens/<module>.py``."""
    canonical = f"{TOKENS_PKG}.{module}"
    targets = {canonical}
    for legacy, real in _REDIRECT_MAP.items():
        if real == canonical:
            targets.add(legacy if legacy.startswith("bernstein.") else f"bernstein.core.{legacy}")
    return targets


def _excluded_from_scan_under(root: Path) -> set[Path]:
    return {root / "src" / "bernstein" / "core" / "__init__.py"}


def _scanned_files(root: Path) -> list[Path]:
    excluded = _excluded_from_scan_under(root)
    return [p for p in (root / "src").rglob("*.py") if p not in excluded]


@lru_cache(maxsize=8)
def _import_index(root: Path) -> tuple[dict[str, list[Path]], dict[tuple[str, str], list[Path]]]:
    """One AST pass over ``src`` instead of one per module.

    Cached per ``root`` (rather than a bare no-argument cache) so scanning an
    alternate worktree for #5552's branch-only comparison does not evict or
    collide with the result for ``REPO_ROOT``. Rebuilt once per session per
    root; the naive shape re-parsed the whole tree for each of the 30-odd
    modules and cost over a minute of CI wall time.
    """
    dotted: dict[str, list[Path]] = {}
    from_package: dict[tuple[str, str], list[Path]] = {}

    for path in _scanned_files(root):
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


def _importer_of(module: str, root: Path = REPO_ROOT) -> Path | None:
    targets = _import_targets(module)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = _tokens_dir_under(root) / f"{module}.py"
    dotted, from_package = _import_index(root)

    for target in targets:
        for path in dotted.get(target, ()):
            if path != own_file:
                return path
    for package in packages:
        for path in from_package.get((package, module), ()):
            if path != own_file:
                return path
    return None


def _importers_of(module: str, root: Path = REPO_ROOT) -> set[Path]:
    """Every file that imports `module`, by any of its resolvable paths."""
    targets = _import_targets(module)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = _tokens_dir_under(root) / f"{module}.py"
    dotted, from_package = _import_index(root)

    found: set[Path] = set()
    for target in targets:
        found |= {p for p in dotted.get(target, ()) if p != own_file}
    for package in packages:
        found |= {p for p in from_package.get((package, module), ()) if p != own_file}
    return found


def reachable_modules(importers: dict[str, set[Path]], tokens_dir: Path = TOKENS_DIR) -> set[str]:
    """Modules reachable from a caller outside ``core/tokens/``.

    Having an importer is not the same as being reachable: two dead modules
    that import each other each have one, and a scan that stops at "somebody
    imports it" reports both as live. Seed from callers outside the package
    and close over intra-package edges instead.
    """
    reachable = {name for name, paths in importers.items() if any(tokens_dir not in path.parents for path in paths)}
    grew = True
    while grew:
        grew = False
        for name, paths in importers.items():
            if name in reachable:
                continue
            if any(p.parent == tokens_dir and p.stem in reachable for p in paths):
                reachable.add(name)
                grew = True
    return reachable


def _current_orphans(root: Path = REPO_ROOT) -> set[str]:
    importers = {name: _importers_of(name, root) for name in _module_names(root)}
    return set(importers) - reachable_modules(importers, tokens_dir=_tokens_dir_under(root))


def test_no_new_orphan_token_modules() -> None:
    """The set of caller-less modules may shrink, never grow (#5552).

    Reports both drift directions in one message, and -- when the branch's
    own pre-merge tip is resolvable (see ``_orphan_scan.py``) -- states
    plainly when the drift belongs to the default branch rather than to
    this change.
    """
    current = _current_orphans()

    branch_ref = resolve_branch_only_ref(REPO_ROOT)
    branch_only = scan_at_ref(branch_ref, REPO_ROOT, _current_orphans) if branch_ref else None

    message = describe_ratchet_drift(
        baseline=KNOWN_ORPHANS,
        current=current,
        branch_only=branch_only,
        guard_name="core/tokens/",
        wire_hint="Wire it to a consumer that exists today, or delete the module together "
        "with its tests and its bernstein/core/__init__.py alias entry.",
    )
    assert message is None, message


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
