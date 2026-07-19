"""Sovereign-profile self-check battery powering ``bernstein doctor sovereign``.

Extends the renderer pattern of the air-gap doctor
(:mod:`bernstein.core.distribution.doctor_airgap`) to the composed sovereign
posture. Each check answers one question a residency-constrained operator's
auditor asks:

1. "Is egress actually shut?" -> airgap network posture (deny-all + socket
   guard), reused verbatim from the air-gap battery.
2. "Does the live config satisfy the residency posture?" -> local storage,
   offline catalog, strict EU residency, certified endpoints.
3. "Is the posture attested and has it drifted?" -> the on-disk attestation
   verifies, and the live posture hash still matches it.

Checks are pure functions returning a :class:`Check` row; the CLI renderer
formats the report, renders the live posture against the attestation, and
chooses the exit code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.distribution.doctor_airgap import (
    Check,
    CheckStatus,
    check_audit_chain_hmac,
    check_network_policy_deny_all,
    check_runtime_socket_guard_active,
)
from bernstein.core.security.deployment_profile import (
    SOVEREIGN_PROFILE,
    endpoint_certification_violations,
    evaluate_posture_drift,
    load_config_snapshot,
    resolve_effective_policy,
)
from bernstein.core.security.network_policy import ENV_SOVEREIGN_MODE

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.deployment_profile import DriftEvaluation, EffectivePolicy


@dataclass(frozen=True)
class SovereignReport:
    """Aggregate sovereign self-check report.

    ``ok`` is True iff there are zero FAIL rows. WARN rows do not block
    ``ok=True`` (for example an un-attested-yet posture on a clean host is a
    WARN, not a FAIL).
    """

    ok: bool
    posture_hash: str = ""
    attested_hash: str = ""
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @classmethod
    def from_checks(cls, rows: list[Check], *, posture_hash: str, attested_hash: str) -> SovereignReport:
        ok = all(row.status is not CheckStatus.FAIL for row in rows)
        return cls(ok=ok, posture_hash=posture_hash, attested_hash=attested_hash, checks=tuple(rows))


def check_sovereign_profile_active() -> Check:
    """Verify the sovereign marker *pair* consistently marked this process tree."""
    from bernstein.core.security.network_policy import SovereignMarkerError, is_sovereign_profile

    try:
        active = is_sovereign_profile()
    except SovereignMarkerError as exc:
        return Check(
            name="sovereign profile active",
            status=CheckStatus.FAIL,
            detail=f"markers are inconsistent: {exc}",
            fix="rerun with --profile sovereign so both profile markers are installed together",
        )
    if active:
        return Check(
            name="sovereign profile active",
            status=CheckStatus.PASS,
            detail=f"{ENV_SOVEREIGN_MODE}={os.environ.get(ENV_SOVEREIGN_MODE)}",
        )
    return Check(
        name="sovereign profile active",
        status=CheckStatus.FAIL,
        detail=f"{ENV_SOVEREIGN_MODE} unset (operator did not invoke --profile sovereign)",
        fix="rerun with --profile sovereign",
    )


def check_storage_local(policy: EffectivePolicy) -> Check:
    """Verify the configured storage backend keeps artifacts on local disk."""
    from bernstein.core.security.deployment_profile import LOCAL_STORAGE_BACKENDS

    if policy.storage_backend in LOCAL_STORAGE_BACKENDS:
        return Check(
            name="storage backend local",
            status=CheckStatus.PASS,
            detail=f"storage.backend={policy.storage_backend} (local sink)",
        )
    return Check(
        name="storage backend local",
        status=CheckStatus.FAIL,
        detail=f"storage.backend={policy.storage_backend} is a remote sink",
        fix=f"set storage.backend to a local backend {sorted(LOCAL_STORAGE_BACKENDS)}",
    )


def check_catalog_offline(policy: EffectivePolicy) -> Check:
    """Verify no catalog source is enabled (offline catalog mode)."""
    enabled = [str(c.get("name")) for c in policy.catalogs if c.get("enabled")]
    if not enabled:
        return Check(
            name="catalog offline",
            status=CheckStatus.PASS,
            detail="no enabled catalog sources",
        )
    return Check(
        name="catalog offline",
        status=CheckStatus.FAIL,
        detail=f"{len(enabled)} catalog(s) enabled: {', '.join(enabled)}",
        fix="disable every catalog source (offline catalog mode) in bernstein.yaml",
    )


def check_residency_strict_eu(policy: EffectivePolicy) -> Check:
    """Verify strict enforcement and EU-only residency regions."""
    from bernstein.core.security.deployment_profile import SOVEREIGN_EU_REGIONS

    outside = sorted(r for r in policy.residency_regions if r not in SOVEREIGN_EU_REGIONS)
    if not policy.residency_enforce_strict:
        return Check(
            name="residency strict EU",
            status=CheckStatus.FAIL,
            detail="residency enforcement is not strict",
            fix="set sovereign.enforce_strict: true in bernstein.yaml",
        )
    if outside or not policy.residency_regions:
        return Check(
            name="residency strict EU",
            status=CheckStatus.FAIL,
            detail=f"regions {list(policy.residency_regions)} are not the EU set {sorted(SOVEREIGN_EU_REGIONS)}",
            fix="pin sovereign.regions to the EU set",
        )
    return Check(
        name="residency strict EU",
        status=CheckStatus.PASS,
        detail=f"strict enforcement, regions={list(policy.residency_regions)}",
    )


def check_endpoints_certified(policy: EffectivePolicy, workdir: Path) -> Check:
    """Verify every gated remote endpoint carries a verified certification receipt."""
    host_problems = [p for p in policy.violations() if "endpoint" in p]
    cert_problems = endpoint_certification_violations(policy, workdir=workdir)
    problems = host_problems + cert_problems
    if not policy.model_endpoints:
        return Check(
            name="endpoints certified/EU",
            status=CheckStatus.PASS,
            detail="no per-role endpoints declared",
        )
    if problems:
        head = "; ".join(problems[:3])
        more = "" if len(problems) <= 3 else f" (+{len(problems) - 3} more)"
        return Check(
            name="endpoints certified/EU",
            status=CheckStatus.FAIL,
            detail=f"{len(problems)} endpoint issue(s): {head}{more}",
            fix="certify each endpoint (bernstein doctor --endpoint ...) or repoint the role to an EU/local endpoint",
        )
    return Check(
        name="endpoints certified/EU",
        status=CheckStatus.PASS,
        detail=f"{len(policy.model_endpoints)} endpoint(s) certified-local or EU-region",
    )


def check_posture_attested(policy: EffectivePolicy, evaluation: DriftEvaluation) -> Check:
    """Verify the posture is attested and the live posture still holds.

    An attestation that exists but is *not trusted* (incomplete contract, bad
    signature, foreign signer) is a FAIL, not the never-activated WARN. Both
    leave ``attested_hash`` empty, so keying off that alone would report a
    tampered record as "you have not activated yet" - a clean bill of health on
    the one surface an auditor reads, at the moment the spawn gate is refusing
    every spawn.

    A matching attested hash is necessary but not sufficient: the spawn gate
    refuses on any :attr:`DriftEvaluation.should_refuse`, which is drift *or* a
    live compliance violation. The attested-equals-enforced egress invariant and
    a certification receipt revoked without a config change both leave the
    posture hash unchanged, so keying off drift alone reported PASS while the
    gate refused every spawn. The row now reports the same live violations the
    gate acts on, so the verifier surface cannot claim the guarantee holds when
    the enforcement surface denies it.
    """
    if evaluation.attestation_rejected:
        return Check(
            name="posture attested (no drift)",
            status=CheckStatus.FAIL,
            detail=f"attestation present but not trusted: {evaluation.attestation_rejected}",
            fix=(
                "investigate the attestation record; re-activate with "
                "'bernstein run --profile sovereign' only after establishing why it was replaced"
            ),
        )
    if not evaluation.attested_hash:
        return Check(
            name="posture attested (no drift)",
            status=CheckStatus.WARN,
            detail=f"no attestation yet; activate to attest posture {policy.posture_hash()}",
            fix="run 'bernstein run --profile sovereign' once to sign and anchor the attestation",
        )
    if evaluation.drifted:
        diverging = ", ".join(evaluation.diverging_keys) or "(unknown)"
        return Check(
            name="posture attested (no drift)",
            status=CheckStatus.FAIL,
            detail=f"drift from attested {evaluation.attested_hash}: diverging keys [{diverging}]",
            fix="restore the intended posture, then re-activate --profile sovereign",
        )
    if evaluation.violations:
        joined = "; ".join(evaluation.violations)
        return Check(
            name="posture attested (no drift)",
            status=CheckStatus.FAIL,
            detail=(
                f"attested {evaluation.attested_hash} still matches the config hash, but the live "
                f"posture violates the sovereign profile: {joined}"
            ),
            fix=(
                "realign the enforced posture with the attestation (for example restore the attested "
                "egress policy or the revoked certification receipt), then re-activate --profile sovereign"
            ),
        )
    return Check(
        name="posture attested (no drift)",
        status=CheckStatus.PASS,
        detail=f"live posture matches attested {evaluation.attested_hash}",
    )


def run_sovereign_checks(workdir: Path | None = None) -> SovereignReport:
    """Run the full sovereign battery and return the aggregate report.

    Args:
        workdir: Project root. Defaults to the current working directory.
    """
    from pathlib import Path

    from bernstein.core.security.deployment_profile import SovereignConfigError
    from bernstein.core.security.network_policy import policy_from_env

    cwd = workdir or Path.cwd()
    config_rows: list[Check] = []
    config_violations: tuple[str, ...] = ()
    try:
        snapshot: dict[str, object] | None = load_config_snapshot(cwd, require=True)
    except SovereignConfigError as exc:
        # Report the fail-closed condition instead of silently reporting on the
        # permissive default posture an unreadable config would project to.
        snapshot = None
        config_violations = (str(exc),)
        config_rows.append(
            Check(
                name="source configuration readable",
                status=CheckStatus.FAIL,
                detail=str(exc),
                fix="restore a readable bernstein.yaml in the project root",
            )
        )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, snapshot)
    # Carry the config failure into the drift evaluation too, or the attestation
    # row could report the empty-config projection as a clean match.
    #
    # Pass the runtime policy explicitly, exactly as the spawn gate does, rather
    # than letting the evaluator derive it. The evaluator only derives one under
    # the airgap marker, so a process whose markers were stripped would fall to
    # ``_live_runtime_policy() is None`` and skip the attested-equals-enforced
    # egress invariant while ``policy_from_env`` sits at allow-all. The verifier
    # must report the same mismatch the gate refuses on, not a clean match.
    evaluation = evaluate_posture_drift(
        workdir=cwd,
        config_snapshot=snapshot,
        runtime_policy=policy_from_env(),
        extra_violations=config_violations,
    )
    rows: list[Check] = [
        *config_rows,
        check_sovereign_profile_active(),
        check_network_policy_deny_all(),
        check_runtime_socket_guard_active(),
        check_storage_local(policy),
        check_catalog_offline(policy),
        check_residency_strict_eu(policy),
        check_endpoints_certified(policy, cwd),
        check_posture_attested(policy, evaluation),
        check_audit_chain_hmac(cwd),
    ]
    return SovereignReport.from_checks(
        rows,
        posture_hash=policy.posture_hash(),
        attested_hash=evaluation.attested_hash,
    )


__all__ = [
    "SovereignReport",
    "check_catalog_offline",
    "check_endpoints_certified",
    "check_posture_attested",
    "check_residency_strict_eu",
    "check_sovereign_profile_active",
    "check_storage_local",
    "run_sovereign_checks",
]
