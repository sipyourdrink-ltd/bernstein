"""No module under ``core/tokens/`` or ``core/security/`` may sit in the tree with no caller.

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

import pytest

from bernstein.core import _REDIRECT_MAP

#: Scans the source tree rather than importing it, so no diff produces an
#: import edge to this file. The marker puts it in every pull request's
#: affected slice instead of only the merge group (#5428).
pytestmark = pytest.mark.whole_tree_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS_DIR = REPO_ROOT / "src" / "bernstein" / "core" / "tokens"
TOKENS_PKG = "bernstein.core.tokens"
SECURITY_DIR = REPO_ROOT / "src" / "bernstein" / "core" / "security"
SECURITY_PKG = "bernstein.core.security"

# The alias table is a redirect declaration, not a call site.
EXCLUDED_FROM_SCAN = {REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py"}

# Modules known to have no caller today, kept as an exact set rather than a
# floor: a new orphan fails this test, and removing one of these fails it too
# until the name is struck from the list. The list only ever shrinks.
KNOWN_ORPHANS_TOKENS = frozenset(
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

# Known orphans in core/security/ - the 47 modules with no non-test caller as of #5100.
# Like KNOWN_ORPHANS_TOKENS, this is an exact set: a new orphan fails, and removing one
# of these fails until struck from the list. The list only ever shrinks.
KNOWN_ORPHANS_SECURITY = frozenset(
    {
        "authzen",
        "capability_delta",
        "claude_permission_profiles",
        "command_allowlist",
        "command_policy",
        "commit_signing",
        "compliance_report",
        "data_residency",
        "directory_bridge",
        "directory_registry",
        "dlp_scanner_v2",
        "dp_telemetry",
        "engagement_mandate",
        "environment_digest",
        "external_policy_hook",
        "hipaa",
        "identity_spawn_anchor",
        "ip_allowlist",
        "key_rotation_support",
        "license_manager",
        "native_toolcall_evidence",
        "oauth_pkce",
        "permission_graph",
        "permission_matrix",
        "policy",
        "policy_limits",
        "policy_templates",
        "promptware_ingest",
        "quarantined_parser",
        "rbac",
        "sandbox_escape_detector",
        "sandbox_profiles",
        "seccomp_profiles",
        "seccomp_sandbox",
        "secret_rotation",
        "security_correlation",
        "security_incident_response",
        "sensitive_data",
        "sensitive_file_detector",
        "soc2_report",
        "sso_oidc",
        "state_encryption",
        "surface_grant_delta",
        "tenant_isolation_verify",
        "tenant_rate_limiter",
        "vault_injector",
        "vuln_disclosure",
    }
)


def _package_dir_under(root: Path, package: str) -> Path:
    if package == "tokens":
        return root / "src" / "bernstein" / "core" / "tokens"
    elif package == "security":
        return root / "src" / "bernstein" / "core" / "security"
    else:
        raise ValueError(f"Unknown package: {package}")


def _module_names(root: Path, package_dir: Path) -> list[str]:
    return sorted(p.stem for p in package_dir.glob("*.py") if p.name != "__init__.py")


def _import_targets(module: str, package_name: str) -> set[str]:
    """Every dotted path that resolves to the target module."""
    canonical = f"{package_name}.{module}"
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


def _importer_of(module: str, package_name: str, package_dir: Path, root: Path = REPO_ROOT) -> Path | None:
    targets = _import_targets(module, package_name)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = package_dir / f"{module}.py"
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


def _importers_of(module: str, package_name: str, package_dir: Path, root: Path = REPO_ROOT) -> set[Path]:
    """All files that import this module, excluding the module itself."""
    targets = _import_targets(module, package_name)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = package_dir / f"{module}.py"
    dotted, from_package = _import_index(root)

    found: set[Path] = set()
    for target in targets:
        found |= {p for p in dotted.get(target, ()) if p != own_file}
    for package in packages:
        found |= {p for p in from_package.get((package, module), ()) if p != own_file}
    return found


def reachable_modules(importers: dict[str, set[Path]], package_dir: Path) -> set[str]:
    """Modules reachable from a caller outside the package.

    Having an importer is not the same as being reachable: two dead modules
    that import each other each have one, and a scan that stops at "somebody
    imports it" reports both as live. Seed from callers outside the package
    and close over intra-package edges instead.
    """
    reachable = {name for name, paths in importers.items() if any(package_dir not in path.parents for path in paths)}
    grew = True
    while grew:
        grew = False
        for name, paths in importers.items():
            if name in reachable:
                continue
            if any(p.parent == package_dir and p.stem in reachable for p in paths):
                reachable.add(name)
                grew = True
    return reachable


def _current_orphans(root: Path, package_dir: Path, package_name: str) -> set[str]:
    importers = {name: _importers_of(name, package_name, package_dir, root) for name in _module_names(root, package_dir)}
    return set(importers) - reachable_modules(importers, package_dir=package_dir)


def test_no_new_orphan_token_modules() -> None:
    """The set of caller-less modules may shrink, never grow (#5552).

    Reports both drift directions in one message, and -- when the branch's
    own pre-merge tip is resolvable (see ``_orphan_scan.py``) -- states
    plainly when the drift belongs to the default branch rather than to
    this change.

    The ``_orphan_scan`` import is deferred to inside this function rather
    than sitting at module level: ``scripts/check_test_count_drop.py``
    collects a touched test module in isolation, as a lone file with no
    sibling ``tests/unit/`` package around it, so a module-level cross-file
    import would fail under ``--collect-only`` even though it resolves fine
    in the real tree -- reported as a false "test count dropped to zero"
    rather than the import-path artefact it actually is. A local import runs
    only when the test executes, which collection never does.
    """
    from tests.unit._orphan_scan import describe_ratchet_drift, resolve_branch_only_ref, scan_at_ref

    current = _current_orphans(REPO_ROOT, TOKENS_DIR, TOKENS_PKG)

    branch_ref = resolve_branch_only_ref(REPO_ROOT)
    branch_only = scan_at_ref(branch_ref, REPO_ROOT, lambda r: _current_orphans(r, _package_dir_under(r, "tokens"), TOKENS_PKG)) if branch_ref else None

    message = describe_ratchet_drift(
        baseline=KNOWN_ORPHANS_TOKENS,
        current=current,
        branch_only=branch_only,
        guard_name="core/tokens/",
        wire_hint="Wire it to a consumer that exists today, or delete the module together "
        "with its tests and its bernstein/core/__init__.py alias entry.",
    )
    assert message is None, message


def test_no_new_orphan_security_modules() -> None:
    """The set of caller-less security modules may shrink, never grow (#5100).

    Generalized from the tokens guard: ``core/security/`` holds 145 modules,
    and 34+ have no non-test caller. Reachability is computed the same way --
    ``_REDIRECT_MAP`` aliases are resolved, ``core/__init__.py`` itself is
    excluded, and intra-package edges are closed over so mutually-importing
    dead clusters don't vouch for each other.
    """
    from tests.unit._orphan_scan import describe_ratchet_drift, resolve_branch_only_ref, scan_at_ref

    current = _current_orphans(REPO_ROOT, SECURITY_DIR, SECURITY_PKG)

    branch_ref = resolve_branch_only_ref(REPO_ROOT)
    branch_only = scan_at_ref(branch_ref, REPO_ROOT, lambda r: _current_orphans(r, _package_dir_under(r, "security"), SECURITY_PKG)) if branch_ref else None

    message = describe_ratchet_drift(
        baseline=KNOWN_ORPHANS_SECURITY,
        current=current,
        branch_only=branch_only,
        guard_name="core/security/",
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
    assert "token_monitor" in _module_names(REPO_ROOT, TOKENS_DIR)
    assert _importer_of("token_monitor", TOKENS_PKG, TOKENS_DIR) is not None


def test_the_alias_table_alone_does_not_count_as_a_caller() -> None:
    """Otherwise every module listed in the redirect map reads as reachable."""
    assert KNOWN_ORPHANS_TOKENS, "the guard needs at least one known orphan to be meaningful"
    orphan = sorted(KNOWN_ORPHANS_TOKENS)[0]
    alias_table = (REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{orphan}"' in alias_table, f"{orphan} is expected to be listed in the alias table"
    assert _importer_of(orphan, TOKENS_PKG, TOKENS_DIR) is None


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
    assert reachable_modules(importers, TOKENS_DIR) == {"wired"}


def test_a_module_used_only_by_a_wired_module_is_reachable() -> None:
    """Positive control: intra-package edges do carry reachability."""
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "helper": {TOKENS_DIR / "wired.py"},
    }
    assert reachable_modules(importers, TOKENS_DIR) == {"wired", "helper"}
