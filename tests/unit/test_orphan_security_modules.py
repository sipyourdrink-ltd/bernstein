"""No module under ``core/security/`` may sit in the tree with no caller.

``core/security/`` holds the compliance- and attestation-relevant modules -
HIPAA, SOC 2, DLP, SSO, commit signing, incident response - and most of them
are named in the ``_REDIRECT_MAP`` alias table in
``bernstein/core/__init__.py``. To any check that asks only "is this name
imported somewhere", the alias entry looks like a dependency, so a module
nothing has wired to a command reads as live. This guard resolves the alias
table instead of trusting it, and fails CI on the next such module.

A caller-less module under ``core/security/`` is a defect, not a staging
area (#5505). Every entry in :data:`KNOWN_ORPHAN_REASONS` carries a
machine-checkable reason it is still on the list: an issue number that
will wire or delete it, or a dated ``remove-by:`` schedule. A bare name
is not a decision.

Modeled on :mod:`tests.unit.test_token_orphans`; the two-way ratchet
message comes from :func:`tests.unit._orphan_scan.describe_ratchet_drift`.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import pytest

from bernstein.core import _REDIRECT_MAP

#: Scans the source tree rather than importing it, so no diff produces an
#: import edge to this file. The marker puts it in every pull request's
#: affected slice instead of only the merge group (#5428).
pytestmark = pytest.mark.whole_tree_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_DIR = REPO_ROOT / "src" / "bernstein" / "core" / "security"
SECURITY_PKG = "bernstein.core.security"

# The alias table is a redirect declaration, not a call site.
EXCLUDED_FROM_SCAN = {REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py"}

#: Valid reason shapes (#5505). Issue refs schedule wiring/deletion work;
#: ``remove-by:YYYY-MM-DD`` schedules removal. Free text is rejected so the
#: annotation cannot rot into "we'll get to it".
_ISSUE_REASON_RE = re.compile(r"^#\d+(,#\d+)*$")
_REMOVE_BY_REASON_RE = re.compile(r"^remove-by:(\d{4}-\d{2}-\d{2})$")

# Modules with no caller today. Keys are the shrink-only ratchet set; values
# are the machine-checkable reason each entry is still here. Wiring or
# deleting a module strikes its key in the same PR. Do not add entries
# without a reason the contract test accepts.
#
# Known mappings from #5505:
#   authzen, external_policy_hook -> #4912
#   directory_bridge, directory_registry, sso_oidc, vault_injector, rbac
#     -> Directory and secrets bridges (#4972, #5018, #5021, #5040)
#   hipaa, soc2_report, compliance_report -> #5098
# Everything else is triaged for removal by 2026-12-01 (v4.0.0 window)
# rather than left as an unowned freezer entry.
KNOWN_ORPHAN_REASONS: dict[str, str] = {
    "authzen": "#4912",
    "capability_delta": "remove-by:2026-12-01",
    "claude_permission_profiles": "remove-by:2026-12-01",
    "command_allowlist": "remove-by:2026-12-01",
    "command_policy": "remove-by:2026-12-01",
    "commit_signing": "remove-by:2026-12-01",
    "compliance_report": "#5098",
    "data_residency": "remove-by:2026-12-01",
    "directory_bridge": "#4972,#5018,#5021,#5040",
    "directory_registry": "#4972,#5018,#5021,#5040",
    "dlp_scanner_v2": "remove-by:2026-12-01",
    "dp_telemetry": "remove-by:2026-12-01",
    "engagement_mandate": "remove-by:2026-12-01",
    "environment_digest": "remove-by:2026-12-01",
    "external_policy_hook": "#4912",
    "hipaa": "#5098",
    "identity_spawn_anchor": "remove-by:2026-12-01",
    "ip_allowlist": "remove-by:2026-12-01",
    "key_rotation_support": "remove-by:2026-12-01",
    "license_manager": "remove-by:2026-12-01",
    "native_toolcall_evidence": "remove-by:2026-12-01",
    "oauth_pkce": "remove-by:2026-12-01",
    "permission_graph": "remove-by:2026-12-01",
    "permission_matrix": "remove-by:2026-12-01",
    "policy": "remove-by:2026-12-01",
    "policy_limits": "remove-by:2026-12-01",
    "policy_templates": "remove-by:2026-12-01",
    "promptware_ingest": "remove-by:2026-12-01",
    "quarantined_parser": "remove-by:2026-12-01",
    "rbac": "#4972,#5018,#5021,#5040",
    "sandbox_escape_detector": "remove-by:2026-12-01",
    "sandbox_profiles": "remove-by:2026-12-01",
    "seccomp_profiles": "remove-by:2026-12-01",
    "seccomp_sandbox": "remove-by:2026-12-01",
    "secret_rotation": "remove-by:2026-12-01",
    "security_correlation": "remove-by:2026-12-01",
    "security_incident_response": "remove-by:2026-12-01",
    "sensitive_data": "remove-by:2026-12-01",
    "sensitive_file_detector": "remove-by:2026-12-01",
    "soc2_report": "#5098",
    "sso_oidc": "#4972,#5018,#5021,#5040",
    "state_encryption": "remove-by:2026-12-01",
    "surface_grant_delta": "remove-by:2026-12-01",
    "tenant_isolation_verify": "remove-by:2026-12-01",
    "tenant_rate_limiter": "remove-by:2026-12-01",
    "vault_injector": "#4972,#5018,#5021,#5040",
    "vuln_disclosure": "remove-by:2026-12-01",
}

KNOWN_ORPHANS: frozenset[str] = frozenset(KNOWN_ORPHAN_REASONS)


def orphan_reason_is_valid(reason: str, *, today: date | None = None) -> bool:
    """Return whether ``reason`` is a machine-checkable orphan annotation.

    Accepted shapes:

    * ``#NNNN`` or ``#NNNN,#MMMM,...`` — issue(s) that schedule wire-or-delete.
    * ``remove-by:YYYY-MM-DD`` — dated removal; the date must be today or
      later. An elapsed schedule without striking the entry is the freezer
      again, so past dates fail.

    Free-text reasons are rejected.
    """

    if _ISSUE_REASON_RE.fullmatch(reason):
        return True
    match = _REMOVE_BY_REASON_RE.fullmatch(reason)
    if match is None:
        return False
    deadline = date.fromisoformat(match.group(1))
    as_of = today if today is not None else date.today()
    return deadline >= as_of


def _security_dir_under(root: Path) -> Path:
    return root / "src" / "bernstein" / "core" / "security"


def _module_names(root: Path = REPO_ROOT) -> list[str]:
    return sorted(p.stem for p in _security_dir_under(root).glob("*.py") if p.name != "__init__.py")


def _import_targets(module: str) -> set[str]:
    """Every dotted path that resolves to ``core/security/<module>.py``."""

    canonical = f"{SECURITY_PKG}.{module}"
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
    """One AST pass over ``src`` instead of one per module."""

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
    own_file = _security_dir_under(root) / f"{module}.py"
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
    """Every file that imports ``module``, by any of its resolvable paths."""

    targets = _import_targets(module)
    packages = {t.rsplit(".", 1)[0] for t in targets}
    own_file = _security_dir_under(root) / f"{module}.py"
    dotted, from_package = _import_index(root)

    found: set[Path] = set()
    for target in targets:
        found |= {p for p in dotted.get(target, ()) if p != own_file}
    for package in packages:
        found |= {p for p in from_package.get((package, module), ()) if p != own_file}
    return found


def reachable_modules(importers: dict[str, set[Path]], security_dir: Path = SECURITY_DIR) -> set[str]:
    """Modules reachable from a caller outside ``core/security/``.

    Having an importer is not the same as being reachable: two dead modules
    that import each other each have one, and a scan that stops at "somebody
    imports it" reports both as live. Seed from callers outside the package
    and close over intra-package edges instead.
    """

    reachable = {name for name, paths in importers.items() if any(security_dir not in path.parents for path in paths)}
    grew = True
    while grew:
        grew = False
        for name, paths in importers.items():
            if name in reachable:
                continue
            if any(p.parent == security_dir and p.stem in reachable for p in paths):
                reachable.add(name)
                grew = True
    return reachable


def _current_orphans(root: Path = REPO_ROOT) -> set[str]:
    security_dir = _security_dir_under(root)
    importers = {name: _importers_of(name, root) for name in _module_names(root)}
    return set(importers) - reachable_modules(importers, security_dir=security_dir)


def test_every_known_orphan_carries_a_machine_checkable_reason() -> None:
    """Annotation contract (#5505): every entry has a valid reason, not a bare name.

    Asserts the shape of the reasons map, not the specific module names —
    the names move as modules gain callers or are deleted; the contract must
    not.
    """

    assert KNOWN_ORPHAN_REASONS, "the guard needs at least one annotated orphan"
    assert frozenset(KNOWN_ORPHAN_REASONS) == KNOWN_ORPHANS
    invalid = {name: reason for name, reason in KNOWN_ORPHAN_REASONS.items() if not orphan_reason_is_valid(reason)}
    assert not invalid, (
        "every KNOWN_ORPHANS entry needs a machine-checkable reason "
        f"(#NNNN or remove-by:YYYY-MM-DD with a non-past date); invalid: {invalid}"
    )


def test_orphan_reason_contract_rejects_free_text_and_elapsed_schedules() -> None:
    """Positive/negative controls for :func:`orphan_reason_is_valid`."""

    assert orphan_reason_is_valid("#4912")
    assert orphan_reason_is_valid("#4972,#5018,#5021,#5040")
    assert orphan_reason_is_valid("remove-by:2099-01-01", today=date(2026, 9, 14))
    assert orphan_reason_is_valid("remove-by:2026-09-14", today=date(2026, 9, 14))
    assert not orphan_reason_is_valid("will wire later")
    assert not orphan_reason_is_valid("")
    assert not orphan_reason_is_valid("4912")
    assert not orphan_reason_is_valid("remove-by:2020-01-01", today=date(2026, 9, 14))


def test_no_new_orphan_security_modules() -> None:
    """The set of caller-less modules may shrink, never grow (#5505 / #5552).

    Reports both drift directions in one message, and -- when the branch's
    own pre-merge tip is resolvable (see ``_orphan_scan.py``) -- states
    plainly when the drift belongs to the default branch rather than to
    this change.

    The ``_orphan_scan`` import is deferred to inside this function rather
    than sitting at module level: ``scripts/check_test_count_drop.py``
    collects a touched test module in isolation, as a lone file with no
    sibling ``tests/unit/`` package around it, so a module-level cross-file
    import would fail under ``--collect-only`` even though it resolves fine
    in the real tree.
    """

    from tests.unit._orphan_scan import describe_ratchet_drift, resolve_branch_only_ref, scan_at_ref

    current = _current_orphans()

    branch_ref = resolve_branch_only_ref(REPO_ROOT)
    branch_only = scan_at_ref(branch_ref, REPO_ROOT, _current_orphans) if branch_ref else None

    message = describe_ratchet_drift(
        baseline=KNOWN_ORPHANS,
        current=current,
        branch_only=branch_only,
        guard_name="core/security/",
        wire_hint=(
            "Wire it to a consumer that exists today, or delete the module together "
            "with its tests and its bernstein/core/__init__.py alias entry, and strike "
            "it from KNOWN_ORPHAN_REASONS with a machine-checkable reason on every "
            "remaining entry."
        ),
    )
    assert message is None, message


def test_a_wired_module_is_seen_through_its_legacy_alias() -> None:
    """`resource_limits` is reachable only via `bernstein.core.resource_limits`.

    Nothing imports it by its canonical ``core.security`` path. Without
    redirect resolution the scan calls it an orphan, which is how a detector
    ends up demanding the deletion of a module the orchestrator imports.
    """

    assert "resource_limits" in _module_names()
    assert "bernstein.core.resource_limits" in _import_targets("resource_limits")
    assert _importer_of("resource_limits") is not None
    assert "resource_limits" not in _current_orphans()


def test_the_alias_table_alone_does_not_count_as_a_caller() -> None:
    """Otherwise every module listed in the redirect map reads as reachable."""

    assert KNOWN_ORPHANS, "the guard needs at least one known orphan to be meaningful"
    orphan = "hipaa"
    assert orphan in KNOWN_ORPHANS
    alias_table = (REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{orphan}"' in alias_table, f"{orphan} is expected to be listed in the alias table"
    assert _importer_of(orphan) is None


def test_a_mutually_importing_dead_cluster_is_still_orphaned() -> None:
    """Two dead modules that import each other must not vouch for each other."""

    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "dead_a": {SECURITY_DIR / "dead_b.py"},
        "dead_b": {SECURITY_DIR / "dead_a.py"},
    }
    assert reachable_modules(importers) == {"wired"}


def test_a_module_used_only_by_a_wired_module_is_reachable() -> None:
    """Positive control: intra-package edges do carry reachability."""

    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "helper": {SECURITY_DIR / "wired.py"},
    }
    assert reachable_modules(importers) == {"wired", "helper"}


def test_a_one_way_dead_chain_is_orphaned_end_to_end() -> None:
    """A module whose only importer is itself caller-less is an orphan too."""

    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "dead_chain_head": {SECURITY_DIR / "dead_chain_middle.py"},
        "dead_chain_middle": {SECURITY_DIR / "dead_chain_tail.py"},
        "dead_chain_tail": {SECURITY_DIR / "dead_chain_head.py"},
    }
    assert reachable_modules(importers) == {"wired"}
