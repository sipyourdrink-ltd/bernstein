"""Air-gap self-check battery powering ``bernstein doctor airgap``.

Each check answers one of the three questions a sovereign customer's
compliance team will ask during evaluation (per the ticket's mental
model alignment section):

1. "What's running on our infra, with cryptographic provenance?"
   -> wheelhouse manifest + signatures
2. "What did the agent runtime do, and prove the log isn't doctored?"
   -> audit chain HMAC integrity
3. "What happens if this tool tries to call out right now?"
   -> network policy is deny-all and MCP catalog is all-off

Checks are pure functions returning a :class:`Check` row; the CLI
formats the report and chooses the exit code.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from bernstein.core.security.network_policy import (
    ENV_PROFILE_MODE,
    PROFILE_AIRGAP,
    policy_from_env,
)


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"


@dataclass(frozen=True)
class Check:
    """One row in the air-gap self-check report."""

    name: str
    status: CheckStatus
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class AirgapReport:
    """Aggregate self-check report.

    ``ok`` is True iff there are zero FAIL rows. WARN rows do not
    block ``ok=True`` because some checks (memo store path) are
    advisory when the operator overrides defaults legitimately.
    """

    ok: bool
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @classmethod
    def from_checks(cls, rows: list[Check]) -> AirgapReport:
        ok = all(row.status is not CheckStatus.FAIL for row in rows)
        return cls(ok=ok, checks=tuple(rows))


def check_profile_active() -> Check:
    """Verify ``--profile airgap`` was the entry point of this process tree."""
    profile = os.environ.get(ENV_PROFILE_MODE, "").strip().lower()
    if profile == PROFILE_AIRGAP:
        return Check(
            name="airgap profile active",
            status=CheckStatus.PASS,
            detail=f"{ENV_PROFILE_MODE}={PROFILE_AIRGAP}",
        )
    return Check(
        name="airgap profile active",
        status=CheckStatus.FAIL,
        detail=f"{ENV_PROFILE_MODE} unset (operator did not invoke --profile airgap)",
        fix="rerun with --profile airgap",
    )


def check_network_policy_deny_all() -> Check:
    """Verify the active network policy denies every destination by default."""
    policy = policy_from_env()
    if policy.allow_any:
        return Check(
            name="network policy deny-all",
            status=CheckStatus.FAIL,
            detail="policy is allow-any (back-compat default)",
            fix="set --allow-network none or --profile airgap",
        )
    if not policy.rules:
        return Check(
            name="network policy deny-all",
            status=CheckStatus.PASS,
            detail="explicit deny-all (none)",
        )
    return Check(
        name="network policy deny-all",
        status=CheckStatus.WARN,
        detail=f"allow-list active: {','.join(policy.rules)}",
        fix="confirm every entry is on the operator's approved egress list",
    )


def check_mcp_catalog_all_off() -> Check:
    """Verify the user MCP config has no enabled bernstein-managed entries.

    Under ``--profile airgap`` the catalog is asserted-disabled at
    install time, but a residual config from a previous non-airgap
    session would still expose endpoints. This check inspects the
    user's MCP config directly.
    """
    from bernstein.core.protocols.mcp_catalog.user_config import (
        default_user_config_path,
        list_installed,
    )

    cfg_path = default_user_config_path()
    if not cfg_path.exists():
        return Check(
            name="MCP catalog all-off",
            status=CheckStatus.PASS,
            detail=f"no user MCP config at {cfg_path}",
        )
    installed = list_installed(cfg_path)
    if not installed:
        return Check(
            name="MCP catalog all-off",
            status=CheckStatus.PASS,
            detail="bernstein-managed block empty",
        )
    names = ", ".join(entry.id for entry in installed[:5])
    suffix = "" if len(installed) <= 5 else f" (+{len(installed) - 5} more)"
    return Check(
        name="MCP catalog all-off",
        status=CheckStatus.FAIL,
        detail=f"{len(installed)} entry(ies) installed: {names}{suffix}",
        fix="bernstein mcp catalog remove <id> for each, or wipe ~/.config/bernstein/mcp.json",
    )


def check_memo_store_local(workdir: Path) -> Check:
    """Verify the fingerprint memo store is on local disk under .sdd/runtime."""
    expected = workdir / ".sdd" / "runtime" / "memo"
    home_cache = Path.home() / ".cache" / "bernstein"
    if home_cache.exists():
        return Check(
            name="memo store on local disk",
            status=CheckStatus.WARN,
            detail=f"residual cache exists at {home_cache}",
            fix=f"rm -rf {home_cache} (the airgap profile pins the memo to {expected})",
        )
    return Check(
        name="memo store on local disk",
        status=CheckStatus.PASS,
        detail=f"no shared cache; airgap pins memo to {expected}",
    )


def check_audit_chain_hmac(workdir: Path) -> Check:
    """Verify the HMAC chain on the audit log is intact."""
    from bernstein.core.security.audit import AuditKeyPermissionError
    from bernstein.core.security.audit_integrity import verify_audit_integrity

    audit_dir = workdir / ".sdd" / "audit"
    if not audit_dir.exists():
        return Check(
            name="audit chain HMAC valid",
            status=CheckStatus.WARN,
            detail=f"no audit dir at {audit_dir} - nothing to verify yet",
        )
    try:
        result = verify_audit_integrity(audit_dir)
    except AuditKeyPermissionError as exc:
        return Check(
            name="audit chain HMAC valid",
            status=CheckStatus.FAIL,
            detail=f"audit key permissions rejected: {exc}",
            fix="chmod 600 the audit key file",
        )
    if result.valid:
        return Check(
            name="audit chain HMAC valid",
            status=CheckStatus.PASS,
            detail=f"{result.entries_checked} entries verified, chain intact",
        )
    first = result.errors[0] if result.errors else "unknown"
    return Check(
        name="audit chain HMAC valid",
        status=CheckStatus.FAIL,
        detail=f"chain broken - {first}",
        fix="investigate audit log; tampering or corruption suspected",
    )


def check_no_external_hostnames(workdir: Path) -> Check:
    """Grep .sdd/runtime for references to non-loopback hostnames.

    A run that stayed on the operator's hardware should leave no
    cloud-provider hostnames behind in its runtime state. The check
    is intentionally cheap (file scan, no parsing).
    """
    runtime = workdir / ".sdd" / "runtime"
    if not runtime.exists():
        return Check(
            name="no external hostnames in runtime",
            status=CheckStatus.WARN,
            detail=f"no .sdd/runtime at {runtime} - nothing to scan",
        )
    needles = (
        b"api.openai.com",
        b"api.anthropic.com",
        b"generativelanguage.googleapis.com",
        b"api.cloudflare.com",
    )
    offenders: list[str] = []
    for path in runtime.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for needle in needles:
            if needle in data:
                offenders.append(f"{path.relative_to(workdir)} mentions {needle.decode()}")
                break
    if offenders:
        head = "; ".join(offenders[:3])
        more = "" if len(offenders) <= 3 else f" (+{len(offenders) - 3} more)"
        return Check(
            name="no external hostnames in runtime",
            status=CheckStatus.FAIL,
            detail=f"{len(offenders)} file(s) reference public endpoints: {head}{more}",
            fix="purge .sdd/runtime and rerun under --profile airgap",
        )
    return Check(
        name="no external hostnames in runtime",
        status=CheckStatus.PASS,
        detail="no public endpoint references found",
    )


def check_runtime_socket_guard_active() -> Check:
    """Verify the process-wide runtime socket guard can be patched in.

    Under ``--profile airgap`` the orchestrator installs a
    :class:`socket.socket.connect` hook so an un-declared outbound dial
    raises :class:`NetworkPolicyDenied`. The hook only patches when
    ``BERNSTEIN_PROFILE_MODE=airgap`` is set; outside airgap it is a
    no-op.

    Standalone ``bernstein doctor airgap`` is the documented pre-flight
    workflow -- it runs *before* ``bernstein run``. The guard is therefore
    not yet installed in the doctor's own process. Reporting FAIL on
    that legitimate state used to mislead operators (bughunt 2026-05-13).

    We pick option (A) from the bughunt brief: install the guard for
    the duration of this check and uninstall right after. The guard is
    documented idempotent (see ``socket_guard.install_runtime_socket_guard``
    docstring), ``uninstall_runtime_socket_guard()`` fully restores the
    original ``socket.socket.connect``, and the doctor command is a
    short-lived CLI process with no concurrent socket work to disturb.
    Bonus: round-tripping install/uninstall actually *exercises* the
    guard's monkeypatch logic, so a regression in the guard itself
    would surface here -- a strictly stronger signal than option (B)'s
    "code-path inventory only" WARN.

    Outside airgap the check still WARNs (the guard is intentionally a
    no-op there; nothing to assert).
    """
    from bernstein.core.security.socket_guard import (
        install_runtime_socket_guard,
        is_runtime_socket_guard_installed,
        uninstall_runtime_socket_guard,
    )

    if os.environ.get(ENV_PROFILE_MODE, "").strip().lower() != PROFILE_AIRGAP:
        return Check(
            name="runtime socket guard active",
            status=CheckStatus.WARN,
            detail="airgap profile not active in this shell -- guard is not installed",
        )
    if is_runtime_socket_guard_installed():
        return Check(
            name="runtime socket guard active",
            status=CheckStatus.PASS,
            detail="socket.socket.connect is patched to consult the network policy",
        )
    # Pre-flight scenario: airgap profile is set in the operator's
    # shell but ``bernstein run`` has not yet been invoked, so the
    # run-bootstrap installer has not fired. Install the guard
    # ourselves, verify the patch, then restore. If any step fails,
    # surface that as a real FAIL.
    installed_here = False
    try:
        installed_here = install_runtime_socket_guard()
        if not installed_here or not is_runtime_socket_guard_installed():
            return Check(
                name="runtime socket guard active",
                status=CheckStatus.FAIL,
                detail="install_runtime_socket_guard() did not patch socket.socket.connect",
                fix="inspect bernstein.core.security.socket_guard for import-time errors",
            )
        return Check(
            name="runtime socket guard active",
            status=CheckStatus.PASS,
            detail="socket.socket.connect patch verified (installed-and-restored by doctor)",
        )
    finally:
        if installed_here:
            uninstall_runtime_socket_guard()


def check_policy_blocks_known_endpoints() -> Check:
    """Sanity probe: feed every adapter's declared endpoints into the policy.

    A passing check proves the policy actually rejects what the
    adapters would dial out to. The check imports the adapter modules
    lazily so it works in slim test environments.
    """
    declared: list[tuple[str, int, str]] = []
    with suppress(Exception):
        from bernstein.adapters.claude import ClaudeCodeAdapter

        for host, port in ClaudeCodeAdapter.external_endpoints:
            declared.append((host, port, "claude"))
    with suppress(Exception):
        from bernstein.adapters.codex import CodexAdapter

        for host, port in CodexAdapter.external_endpoints:
            declared.append((host, port, "codex"))
    if not declared:
        return Check(
            name="policy blocks declared endpoints",
            status=CheckStatus.WARN,
            detail="no adapter endpoints discoverable in this environment",
        )

    policy = policy_from_env()
    leaks = [f"{host}:{port} ({source})" for host, port, source in declared if policy.is_allowed(host, port)]
    if leaks:
        head = "; ".join(leaks[:3])
        more = "" if len(leaks) <= 3 else f" (+{len(leaks) - 3} more)"
        return Check(
            name="policy blocks declared endpoints",
            status=CheckStatus.FAIL,
            detail=f"{len(leaks)} declared endpoint(s) currently allowed: {head}{more}",
            fix="tighten --allow-network or remove the override",
        )
    return Check(
        name="policy blocks declared endpoints",
        status=CheckStatus.PASS,
        detail=f"{len(declared)} declared endpoint(s) all blocked",
    )


def run_airgap_checks(workdir: Path | None = None) -> AirgapReport:
    """Run the full battery and return the aggregate report.

    Args:
        workdir: Project root. Defaults to the current working directory.
    """
    cwd = workdir or Path.cwd()
    rows: list[Check] = [
        check_profile_active(),
        check_network_policy_deny_all(),
        check_runtime_socket_guard_active(),
        check_policy_blocks_known_endpoints(),
        check_mcp_catalog_all_off(),
        check_memo_store_local(cwd),
        check_audit_chain_hmac(cwd),
        check_no_external_hostnames(cwd),
    ]
    return AirgapReport.from_checks(rows)


__all__ = [
    "AirgapReport",
    "Check",
    "CheckStatus",
    "check_audit_chain_hmac",
    "check_mcp_catalog_all_off",
    "check_memo_store_local",
    "check_network_policy_deny_all",
    "check_no_external_hostnames",
    "check_policy_blocks_known_endpoints",
    "check_profile_active",
    "check_runtime_socket_guard_active",
    "run_airgap_checks",
]
