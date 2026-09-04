"""Signed isolation attestation body (#3278, scope step 1).

The attestation is the object the selector will later have to resolve against,
so the properties pinned here are the ones that make it usable as evidence:
the signed bytes are a pure function of ``{host facts, backend measurements,
install identity}``, nothing in the body can drift run to run, and a capability
that was never measured can never be presented as one that was.

Nothing selects differently because of these tests -- step 1 is inert by
design. What they protect is the shape the later steps build on.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.sandbox_cmd import sandbox_group
from bernstein.core.sandbox.attestation import (
    ATTESTATION_BODY_KEYS,
    BACKEND_ENTRY_KEYS,
    HOST_FACT_KEYS,
    PROBE_ENTRY_KEYS,
    AttestationVerificationError,
    BackendAttestation,
    IsolationAttestationError,
    ProbeOutcome,
    ProbeResult,
    attestation_from_dict,
    build_isolation_attestation,
    verify_isolation_attestation,
)
from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.security.agent_card_keystore import AgentCardKeystore

if TYPE_CHECKING:
    from pathlib import Path

CAP = SandboxCapability

HOST_FACTS: dict[str, Any] = {
    "os": "linux",
    "arch": "x86_64",
    "kernel": "6.1.0-generic",
    "runtime_versions": {"docker": "27.0.3"},
    "runtime_binary_digests": {"docker": "b" * 64},
    "cgroup_version": 2,
    "rootless": False,
}

# Key tokens that would make the signed body vary run to run. The signed body
# must never carry one, at any nesting depth. Matching is on underscore-split
# tokens rather than substrings, so a legitimate key like ``runtime_versions``
# is not read as a wall-clock because it contains the letters "time".
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "chain",
        "clock",
        "created",
        "date",
        "datetime",
        "elapsed",
        "epoch",
        "index",
        "nonce",
        "now",
        "position",
        "seq",
        "sequence",
        "since",
        "time",
        "timestamp",
        "ts",
        "uptime",
    },
)
_FORBIDDEN_KEYS = frozenset({"chain_position", "measured_at", "run_id", "runid"})


def _names_a_varying_quantity(key: str) -> bool:
    low = key.lower()
    return low in _FORBIDDEN_KEYS or bool(_FORBIDDEN_KEY_TOKENS & set(low.split("_")))


@pytest.fixture
def keystore(tmp_path: Path) -> AgentCardKeystore:
    return AgentCardKeystore(tmp_path / "keys")


def _docker_entry() -> BackendAttestation:
    """A backend whose declared set was measured and delivered in full."""
    return BackendAttestation(
        name="docker",
        declared=(CAP.FILE_RW, CAP.EXEC, CAP.NETWORK),
        observed=(CAP.FILE_RW, CAP.EXEC, CAP.NETWORK),
        probes=(
            ProbeResult(capability=CAP.FILE_RW, outcome=ProbeOutcome.PASS),
            ProbeResult(capability=CAP.EXEC, outcome=ProbeOutcome.PASS),
            ProbeResult(capability=CAP.NETWORK, outcome=ProbeOutcome.PASS),
        ),
    )


def _microvm_entry() -> BackendAttestation:
    """A backend that refuted its whole declared set from one probe.

    The construction failure refutes everything the class declares, so the
    probe list is shorter than ``refuted`` -- that asymmetry is real and the
    body has to carry it.
    """
    return BackendAttestation(
        name="microvm",
        declared=(CAP.FILE_RW, CAP.EXEC, CAP.SNAPSHOT),
        refuted=(CAP.FILE_RW, CAP.EXEC, CAP.SNAPSHOT),
        probes=(
            ProbeResult(
                capability=CAP.FILE_RW,
                outcome=ProbeOutcome.FAIL,
                reason_code="microvm_unavailable_no_hypervisor",
            ),
        ),
    )


def _remote_entry() -> BackendAttestation:
    """A remote backend: round-trippable capabilities only, the rest unverifiable."""
    return BackendAttestation(
        name="daytona",
        declared=(CAP.FILE_RW, CAP.EXEC, CAP.SCOPED_MOUNT),
        observed=(CAP.FILE_RW, CAP.EXEC),
        unverifiable=(CAP.SCOPED_MOUNT,),
        probes=(
            ProbeResult(capability=CAP.FILE_RW, outcome=ProbeOutcome.PASS),
            ProbeResult(capability=CAP.EXEC, outcome=ProbeOutcome.PASS),
            ProbeResult(
                capability=CAP.SCOPED_MOUNT,
                outcome=ProbeOutcome.UNVERIFIABLE,
                reason_code="isolation_strength_not_client_observable",
            ),
        ),
    )


def _mint(keystore: AgentCardKeystore) -> Any:
    return build_isolation_attestation(
        keystore=keystore,
        host_facts=HOST_FACTS,
        backends=(_microvm_entry(), _docker_entry(), _remote_entry()),
    )


def _walk_keys(node: object) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            out.append(str(key))
            out.extend(_walk_keys(value))
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            out.extend(_walk_keys(item))
    return out


class TestDeterminism:
    def test_minting_twice_on_unchanged_host_is_byte_identical(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        """Same host facts + same measurements + same identity -> same bytes.

        Compared as bytes, not as parsed dicts: a dict comparison would pass
        even if a field re-ordered or a float re-rendered, and it is the bytes
        that ``host_facts_digest`` and the payload digest are taken over.
        """
        first = _mint(keystore)
        second = _mint(keystore)
        assert first.to_canonical_json().encode("utf-8") == second.to_canonical_json().encode("utf-8")
        assert first.signing_bytes() == second.signing_bytes()
        assert first.signature == second.signature

    def test_backend_order_does_not_change_the_signed_bytes(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        """Lists are canonically ordered; ``sort_keys`` does not sort list items."""
        forward = build_isolation_attestation(
            keystore=keystore,
            host_facts=HOST_FACTS,
            backends=(_docker_entry(), _microvm_entry()),
        )
        reversed_ = build_isolation_attestation(
            keystore=keystore,
            host_facts=HOST_FACTS,
            backends=(_microvm_entry(), _docker_entry()),
        )
        assert forward.signing_bytes() == reversed_.signing_bytes()


class TestBodyShape:
    def test_signed_body_keys_stay_within_the_allowlist(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        """Walk every key in the signed body against the allowlist.

        Asserting on the allowlist rather than on a hand-written list of banned
        fields is what stops a later field from reintroducing a wall-clock, a
        run id, or a chain position without this test noticing.
        """
        body = json.loads(_mint(keystore).signing_bytes().decode("utf-8"))
        assert set(body) == set(ATTESTATION_BODY_KEYS)
        assert set(body["host_facts"]) <= set(HOST_FACT_KEYS)
        for entry in body["backends"]:
            assert set(entry) == set(BACKEND_ENTRY_KEYS)
            for probe in entry["probes"]:
                assert set(probe) == set(PROBE_ENTRY_KEYS)

    def test_no_key_at_any_depth_names_a_varying_quantity(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        body = json.loads(_mint(keystore).signing_bytes().decode("utf-8"))
        offenders = [key for key in _walk_keys(body) if _names_a_varying_quantity(key)]
        assert offenders == []

    def test_unknown_host_fact_key_is_refused_at_mint(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        """Host facts are an allowlist, so a timestamp cannot ride in with them."""
        with pytest.raises(IsolationAttestationError, match="host fact"):
            build_isolation_attestation(
                keystore=keystore,
                host_facts={**HOST_FACTS, "measured_at": 1700000000},
                backends=(_docker_entry(),),
            )


class TestClassificationInvariants:
    def test_unverifiable_capability_cannot_also_be_observed(self) -> None:
        """The three outcome sets are disjoint by construction.

        This is the guard behind the non-claim: nothing can promote a value
        out of ``unverifiable`` into ``observed``, because a body carrying it
        in both places does not construct.
        """
        with pytest.raises(IsolationAttestationError, match="disjoint"):
            BackendAttestation(
                name="daytona",
                declared=(CAP.FILE_RW,),
                observed=(CAP.FILE_RW,),
                unverifiable=(CAP.FILE_RW,),
                probes=(),
            )

    def test_declared_capability_must_be_classified_exactly_once(self) -> None:
        """A declared capability with no verdict is a third, invisible state."""
        with pytest.raises(IsolationAttestationError, match="unclassified"):
            BackendAttestation(
                name="docker",
                declared=(CAP.FILE_RW, CAP.EXEC),
                observed=(CAP.FILE_RW,),
                probes=(),
            )

    def test_undeclared_capability_cannot_be_observed(self) -> None:
        with pytest.raises(IsolationAttestationError, match="not declared"):
            BackendAttestation(
                name="docker",
                declared=(CAP.FILE_RW,),
                observed=(CAP.FILE_RW, CAP.NETWORK),
                probes=(),
            )

    def test_probe_outcome_must_agree_with_the_capability_classification(self) -> None:
        """A passing probe for a refuted capability is a contradiction, not a note."""
        with pytest.raises(IsolationAttestationError, match="contradicts"):
            BackendAttestation(
                name="docker",
                declared=(CAP.EXEC,),
                refuted=(CAP.EXEC,),
                probes=(ProbeResult(capability=CAP.EXEC, outcome=ProbeOutcome.PASS),),
            )

    def test_failed_probe_must_carry_a_reason_code(self) -> None:
        with pytest.raises(IsolationAttestationError, match="reason_code"):
            BackendAttestation(
                name="microvm",
                declared=(CAP.EXEC,),
                refuted=(CAP.EXEC,),
                probes=(ProbeResult(capability=CAP.EXEC, outcome=ProbeOutcome.FAIL),),
            )

    def test_duplicate_backend_names_are_refused(self, keystore: AgentCardKeystore) -> None:
        with pytest.raises(IsolationAttestationError, match="duplicate"):
            build_isolation_attestation(
                keystore=keystore,
                host_facts=HOST_FACTS,
                backends=(_docker_entry(), _docker_entry()),
            )


class TestVerification:
    def test_freshly_minted_attestation_verifies(self, keystore: AgentCardKeystore) -> None:
        verify_isolation_attestation(_mint(keystore))

    def test_tampered_host_facts_digest_fails_with_a_typed_error(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        forged = replace(_mint(keystore), host_facts_digest="0" * 64)
        with pytest.raises(AttestationVerificationError) as excinfo:
            verify_isolation_attestation(forged)
        assert excinfo.value.reason == "host_facts_digest_mismatch"

    def test_tampered_backend_measurement_fails_with_a_typed_error(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        """Flipping microvm's refutation to an observation breaks the signature."""
        minted = _mint(keystore)
        promoted = BackendAttestation(
            name="microvm",
            declared=(CAP.FILE_RW, CAP.EXEC, CAP.SNAPSHOT),
            observed=(CAP.FILE_RW, CAP.EXEC, CAP.SNAPSHOT),
            probes=(),
        )
        forged = replace(minted, backends=(promoted, _docker_entry(), _remote_entry()))
        with pytest.raises(AttestationVerificationError) as excinfo:
            verify_isolation_attestation(forged)
        assert excinfo.value.reason == "signature_invalid"

    def test_keyid_not_matching_the_embedded_key_fails(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        forged = replace(_mint(keystore), keyid="not-the-embedded-thumbprint")
        with pytest.raises(AttestationVerificationError) as excinfo:
            verify_isolation_attestation(forged)
        assert excinfo.value.reason == "keyid_mismatch"

    def test_rotated_install_identity_fails_against_the_current_key_directory(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        from bernstein.core.identity.http_signing import build_key_directory

        minted = _mint(keystore)
        keystore.rotate()
        current = build_key_directory(keystore, include_archived=False)
        with pytest.raises(AttestationVerificationError) as excinfo:
            verify_isolation_attestation(minted, key_directory=current)
        assert excinfo.value.reason == "keyid_not_in_directory"

    def test_dict_roundtrip_preserves_the_digest_and_the_signature(
        self,
        keystore: AgentCardKeystore,
    ) -> None:
        minted = _mint(keystore)
        restored = attestation_from_dict(json.loads(minted.to_canonical_json()))
        verify_isolation_attestation(restored)
        assert restored.attestation_digest() == minted.attestation_digest()
        assert restored.signing_bytes() == minted.signing_bytes()


class TestAttestCli:
    def test_attest_json_emits_an_attestation_that_verifies(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(sandbox_group, ["attest", "--json"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
        restored = attestation_from_dict(payload)
        verify_isolation_attestation(restored)
        assert restored.backends

    def test_attest_claims_nothing_as_observed_before_the_probe_runner_lands(self) -> None:
        """Step 1 measures nothing, so it must attest to nothing.

        An unmeasured capability reported as ``observed`` would be a false
        statement the selector is meant to trust in step 3.
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(sandbox_group, ["attest", "--json"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
        for entry in payload["backends"]:
            assert entry["observed"] == []
            assert entry["refuted"] == []
            assert entry["unverifiable"] == entry["declared"]
