"""Tests for wiring onboarded adapter evidence through the admission chain (issue #3765).

Slice 4 of 4 of #3544: an onboarded adapter's evidence (profile, contract, transcript,
replay result) from the earlier slices feeds `evaluate_admission` unchanged,
producing a real sealed ADMIT or REFUSE receipt through the existing chain.
`generic` is routed through the same ladder.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from bernstein.adapters.admission import (
    ADMISSION_EXEMPT,
    CANARY_GREEN,
    VERDICT_ADMIT,
    AdapterAdmissionReceipt,
    AdmissionEvidence,
    evaluate_admission,
    seal_admission_receipt,
    verify_admission_receipt,
)
from bernstein.adapters.capability_profile import (
    AdapterCapabilityProfile,
    InvocationSpec,
    ProfileImplementation,
    SandboxTier,
)
from bernstein.adapters.conformance import StepResult, TranscriptResult

# -----------------------------------------------------------------------
# Fixtures: evidence shapes from the earlier onboarding slices
# -----------------------------------------------------------------------


class _FakeTranscriptResult(TranscriptResult):
    """Convenience constructor for a one-step transcript result.

    Mirrors bernstein.adapters.conformance.TranscriptResult exactly so
    replay_fingerprint and admission receipt builders see the same shape
    as the real replay output.
    """

    def __init__(self, transcript_name: str, passed: bool) -> None:
        super().__init__(
            transcript_name=transcript_name,
            adapter_class="CLIAdapter",
            step_results=[StepResult(step_index=0, passed=passed, message="")],
        )


def _make_onboarded_profile() -> AdapterCapabilityProfile:
    """A factory-built profile from the onboarding probe step."""
    return AdapterCapabilityProfile(
        name="onboarded-cli",
        display_name="Onboarded CLI",
        invocation=InvocationSpec(binary="onboarded-binary"),
        implementation=ProfileImplementation.FACTORY,
        sandbox=SandboxTier.PROCESS,
    )


def _make_onboarded_evidence(
    *,
    adapter: str = "onboarded-cli",
    binary: str = "onboarded-binary",
    binary_path: str | None = "/usr/local/bin/onboarded-binary",
    installed_version: str = "1.0.0",
    contract_hash: str = "sha256:abc123def456",
    profile_hash: str = "sha256:profile789",
    conformance_verdict: str = "ok",
    conformance_detail: str = "",
    replay_passed: bool = True,
) -> AdmissionEvidence:
    """Build AdmissionEvidence shaped like the earlier slices' output."""
    from bernstein.adapters.admission import replay_fingerprint

    evidence = AdmissionEvidence(
        adapter=adapter,
        binary=binary,
        binary_path=binary_path,
        installed_version=installed_version,
        contract_hash=contract_hash,
        profile_hash=profile_hash,
        conformance_verdict=conformance_verdict,
        conformance_detail=conformance_detail,
        transcript_names=("onboarded_cli_onboard.yaml",),
        replay_passed=replay_passed,
        canary_verdict=CANARY_GREEN,
    )
    # The fingerprint needs the TranscriptResult objects
    fp = replay_fingerprint(
        adapter,
        contract_hash=contract_hash,
        installed_version=installed_version,
        results=[_FakeTranscriptResult("onboarded_cli_onboard.yaml", replay_passed)],
    )
    # Override the fingerprint after construction
    return AdmissionEvidence(
        adapter=evidence.adapter,
        binary=evidence.binary,
        binary_path=evidence.binary_path,
        installed_version=evidence.installed_version,
        contract_hash=evidence.contract_hash,
        profile_hash=evidence.profile_hash,
        conformance_verdict=evidence.conformance_verdict,
        conformance_detail=evidence.conformance_detail,
        transcript_names=evidence.transcript_names,
        replay_passed=evidence.replay_passed,
        canary_verdict=evidence.canary_verdict,
        replay_fingerprint=fp,
    )


# -----------------------------------------------------------------------
# Test: onboarded evidence through evaluate_admission → sealed receipt
# -----------------------------------------------------------------------


def test_onboarded_admit_produces_sealed_receipt(tmp_path: Path) -> None:
    """Feeding onboarded evidence through evaluate_admission produces a sealed ADMIT receipt."""
    evidence = _make_onboarded_evidence()
    decision = evaluate_admission(evidence)

    assert decision.admitted, f"Expected ADMIT but got {decision.reason!r}"
    assert decision.verdict == VERDICT_ADMIT

    stamp = datetime.now(UTC).isoformat()
    sealed_receipt, _sha, path = seal_admission_receipt(
        decision,
        receipts_dir=tmp_path,
        generated_at=stamp,
    )

    assert path.exists()
    loaded_doc = json.loads(path.read_text(encoding="utf-8"))
    assert verify_admission_receipt(loaded_doc), "Sealed receipt must pass verification"

    view = AdapterAdmissionReceipt(sealed_receipt)
    assert view.adapter == "onboarded-cli"
    assert view.verdict == VERDICT_ADMIT
    assert view.replay_fingerprint == evidence.replay_fingerprint


def test_onboarded_refusal_produces_sealed_receipt(tmp_path: Path) -> None:
    """Onboarded evidence with a failing replay produces a sealed REFUSE receipt."""
    evidence = _make_onboarded_evidence(
        conformance_verdict="ok",
        replay_passed=False,
    )
    decision = evaluate_admission(evidence)

    assert not decision.admitted
    assert decision.reason == "replay_diverged"

    stamp = datetime.now(UTC).isoformat()
    sealed_receipt, _sha, path = seal_admission_receipt(
        decision,
        receipts_dir=tmp_path,
        generated_at=stamp,
    )

    assert path.exists()
    loaded_doc = json.loads(path.read_text(encoding="utf-8"))
    assert verify_admission_receipt(loaded_doc), "Refusal receipt must also pass verification"

    view = AdapterAdmissionReceipt(sealed_receipt)
    assert view.verdict != VERDICT_ADMIT


def test_onboarded_profile_hash_is_used_in_evidence() -> None:
    """A factory-built profile's hash is present in the onboarded evidence."""
    profile = _make_onboarded_profile()
    evidence = _make_onboarded_evidence(profile_hash=profile.profile_hash)

    assert evidence.profile_hash == profile.profile_hash


def test_generic_not_in_admission_exempt() -> None:
    """generic is no longer exempt from receipt-gated admission."""
    assert "generic" not in ADMISSION_EXEMPT, (
        "generic must be routed through the admission chain so an onboarded generic adapter's evidence is verified"
    )


# -----------------------------------------------------------------------
# Test: generic adapter goes through the gate with the right signals
# -----------------------------------------------------------------------


def test_generic_adapter_routes_through_gate(
    tmp_path: Path,
) -> None:
    """generic spawn goes through the admission gate with the onboarded signal path."""
    from bernstein.adapters.admission import (
        POLICY_WARN,
        AdmissionGate,
    )

    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()

    # Gate should run for generic now that it's not exempt
    gate = AdmissionGate(receipts_dir=receipts_dir, policy=POLICY_WARN)

    # The gate returns None for mock (exempt) but raises for generic (not exempt)
    # because generic has no contract and no sealed receipt.
    result = gate.admit("generic")

    # generic is not exempt, so the gate returns a decision (not None)
    # It will be a refusal because there's no contract and no sealed receipt.
    assert result is not None, "generic must go through the gate (not exempt)"
    assert not result.admitted, "generic without a contract must be refused"
