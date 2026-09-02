"""No module under ``core/security/`` may sit in the tree with no caller.

``core/security/`` holds the compliance- and attestation-relevant modules -
HIPAA, SOC 2, DLP, SSO, commit signing, incident response - and most of them
are named in the ``_REDIRECT_MAP`` alias table in
``bernstein/core/__init__.py``. To any check that asks only "is this name
imported somewhere", the alias entry looks like a dependency, so a module
nothing has wired to a command reads as live. This guard resolves the alias
table instead of trusting it, and fails CI on the next such module.

The scan itself lives in ``tests/unit/_orphan_scan.py`` and is shared with
``test_token_orphans.py``; only the scanned subpackage and the allowlist
below are local to this file.
"""

from __future__ import annotations

from tests.unit._orphan_scan import REPO_ROOT, SubpackageScan, assert_orphans_match, import_index

SECURITY_DIR = REPO_ROOT / "src" / "bernstein" / "core" / "security"
SCAN = SubpackageScan(directory=SECURITY_DIR, package="bernstein.core.security")

# Modules with no caller today, kept as an exact set rather than a floor: a
# new orphan fails this test, and removing one of these fails it too until
# the name is struck from the list. The list only ever shrinks. Wiring or
# deleting the modules below is tracked separately; this guard exists so the
# list cannot grow while that happens.
KNOWN_ORPHANS = frozenset(
    {
        "audit_export",
        "capability_delta",
        "change_receipt",
        "claude_permission_profiles",
        "command_allowlist",
        "command_policy",
        "commit_signing",
        "compliance_library",
        "compliance_report",
        "data_residency",
        "dlp_scanner_v2",
        "dp_telemetry",
        "dual_approval",
        "engagement_mandate",
        "environment_digest",
        "external_policy_hook",
        "guardrail_pipeline",
        "hipaa",
        "identity_spawn_anchor",
        "ip_allowlist",
        "key_rotation_support",
        "license_manager",
        "native_toolcall_evidence",
        "owasp_asi_detectors",
        "permission_delegation",
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
        "security_posture",
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


def test_no_new_orphan_security_modules() -> None:
    """The set of caller-less modules may shrink, never grow."""
    assert_orphans_match(SCAN, KNOWN_ORPHANS, "core/security/")


def test_a_wired_module_is_seen_through_its_legacy_alias() -> None:
    """`resource_limits` is reachable only via `bernstein.core.resource_limits`.

    Nothing imports it by its canonical ``core.security`` path. Without
    redirect resolution the scan calls it an orphan, which is how a detector
    ends up demanding the deletion of a module the orchestrator, the spawner
    and every adapter import.
    """
    assert "resource_limits" in SCAN.module_names()
    assert "bernstein.core.resource_limits" in SCAN.import_targets("resource_limits")

    dotted, from_package = import_index()
    assert not dotted.get("bernstein.core.security.resource_limits")
    assert not from_package.get(("bernstein.core.security", "resource_limits"))

    assert SCAN.importer_of("resource_limits") is not None
    assert "resource_limits" not in SCAN.orphans()


def test_the_alias_table_alone_does_not_count_as_a_caller() -> None:
    """Otherwise every module listed in the redirect map reads as reachable."""
    alias_table = (REPO_ROOT / "src" / "bernstein" / "core" / "__init__.py").read_text(encoding="utf-8")
    orphan = "hipaa"
    assert orphan in KNOWN_ORPHANS
    assert f'"{orphan}"' in alias_table, f"{orphan} is expected to be listed in the alias table"
    assert SCAN.importer_of(orphan) is None


def test_a_mutually_importing_dead_cluster_is_still_orphaned() -> None:
    """Two dead modules that import each other must not vouch for each other.

    This is the failure the "does anything import it?" shape cannot see, and
    it is how a whole dead corner of a package survives a cleanup.
    """
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "dead_a": {SECURITY_DIR / "dead_b.py"},
        "dead_b": {SECURITY_DIR / "dead_a.py"},
    }
    assert SCAN.reachable_modules(importers) == {"wired"}


def test_a_module_used_only_by_a_wired_module_is_reachable() -> None:
    """Positive control: intra-package edges do carry reachability."""
    live_caller = REPO_ROOT / "src" / "bernstein" / "core" / "orchestration" / "orchestrator.py"
    importers = {
        "wired": {live_caller},
        "helper": {SECURITY_DIR / "wired.py"},
    }
    assert SCAN.reachable_modules(importers) == {"wired", "helper"}
