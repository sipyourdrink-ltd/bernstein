"""A bundle signature is checked, and a bundle signed under another key is refused.

Issue #5856. Two things made `SubmissionBundle.signature` decorative:

1. **Nothing verified it.** `verifier.py` had no reference to `signature` at all. `bench verify`
   checked the suite hash, every receipt hash, the task hashes and the replayed verdicts, and
   never the one field a forger cannot reproduce -- every hash in a bundle is exactly what
   `SubmissionBundle.from_dict` rebuilds, so an internally consistent forgery was indistinguishable
   from an honest bundle.
2. **The production signer could not engage.** `AgentCardSigner.sign` imported
   `bernstein.core.identity.agent_card_signer`, which does not exist (the real one is under
   `core.security`), so its `except ImportError` branch ran on every call and every bundle produced
   without `--stub-signer` was signed by the stub -- HMAC under a public constant in this repo.

The test the first review of #5492 asked for is the third one here: re-sign an honest bundle under
a different key and assert `bench verify` refuses it. It could not be written until both halves
above were fixed.
"""

from __future__ import annotations

import pytest

from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import AgentCardSigner, StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus, verify_signature


def _keypair() -> tuple[bytes, bytes]:
    """An Ed25519 keypair in the PEM shapes the install identity uses."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def suite() -> BenchSuite:
    return BenchSuite(
        version="sig-v1",
        tasks=[
            BenchTask(
                id="task_a",
                description="Task A",
                steps=("step 1",),
                assertions=({"kind": "syntax_valid"},),
                category="cat1",
            )
        ],
    )


@pytest.fixture
def adapter() -> MockReplayAdapter:
    return MockReplayAdapter()


@pytest.fixture
def bundle(suite: BenchSuite, adapter: MockReplayAdapter) -> SubmissionBundle:
    return BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()


class TestTheProductionSignerEngages:
    """The signer used to fall through to the stub on every call."""

    def test_signs_with_the_install_identity_rather_than_the_stub(self, bundle: SubmissionBundle) -> None:
        private_pem, public_pem = _keypair()

        signed = AgentCardSigner(private_pem, public_pem).sign(bundle)

        assert signed.signature
        assert signed.signer_fingerprint != StubSigner.fingerprint()
        assert not signed.signer_fingerprint.endswith("-stub")

    def test_refuses_rather_than_degrading_when_there_is_no_key_material(
        self, bundle: SubmissionBundle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing install identity is an error, not a silent downgrade.

        The old behaviour warned and returned a stub-signed bundle, so the only sign that a
        "signed" artefact was signed with a public key was a warning nobody reads in CI.
        """

        def _no_keystore() -> None:
            raise RuntimeError("no install identity")

        monkeypatch.setattr("bernstein.core.identity.http_signing.default_keystore", _no_keystore)

        with pytest.raises(RuntimeError):
            AgentCardSigner().sign(bundle)


class TestAForgedSignatureIsRefused:
    """The test the first review of #5492 asked for."""

    def test_a_bundle_signed_under_another_key_does_not_verify(self, bundle: SubmissionBundle) -> None:
        honest_private, honest_public = _keypair()
        attacker_private, attacker_public = _keypair()

        honest = AgentCardSigner(honest_private, honest_public).sign(bundle)
        forged = AgentCardSigner(attacker_private, attacker_public).sign(bundle)

        trusted = {honest.signer_fingerprint: honest_public}

        assert verify_signature(honest, trusted) == ""
        assert verify_signature(forged, trusted) != ""

    def test_a_signature_lifted_onto_altered_contents_does_not_verify(self, bundle: SubmissionBundle) -> None:
        """The forgery the issue describes: alter the contents, RECOMPUTE the stored hash, keep
        the signature.

        `from_dict` has an integrity guard, but it compares the stored `bundle_hash` against a
        recomputed one -- so it stops a careless edit and not a forger, who recomputes the stored
        value too. The result is a bundle that is internally consistent in every field. The
        signature is over the bundle hash, so it is the one thing that cannot be recomputed without
        the key, and the only reason the two are distinguishable at all.
        """
        import dataclasses

        private_pem, public_pem = _keypair()
        signed = AgentCardSigner(private_pem, public_pem).sign(bundle)
        trusted = {signed.signer_fingerprint: public_pem}
        assert verify_signature(signed, trusted) == ""

        altered = dataclasses.replace(signed, suite_version="tampered-version")
        # The forgery survives a round trip, which is the point: nothing before the signature
        # check can tell it from the honest bundle.
        rebuilt = SubmissionBundle.from_dict({**altered.to_dict(), "bundle_hash": altered.bundle_hash()})

        assert verify_signature(rebuilt, trusted) != ""

    def test_an_untrusted_fingerprint_is_treated_as_unsigned(self, bundle: SubmissionBundle) -> None:
        """Fail closed. A signature nobody can resolve to a key proves nothing."""
        private_pem, public_pem = _keypair()
        signed = AgentCardSigner(private_pem, public_pem).sign(bundle)

        problem = verify_signature(signed, trusted_keys={})

        assert "does not resolve to a trusted public key" in problem


class TestTheStubIsNotAnAttestation:
    def test_a_stub_bundle_is_refused_unless_the_caller_opted_in(self, bundle: SubmissionBundle) -> None:
        stubbed = StubSigner().sign(bundle)

        assert "STUB" in verify_signature(stubbed, {})
        assert verify_signature(stubbed, {}, allow_stub=True) == ""

    def test_a_forged_stub_signature_still_fails_even_when_stubs_are_allowed(self, bundle: SubmissionBundle) -> None:
        import dataclasses

        stubbed = StubSigner().sign(bundle)
        tampered = dataclasses.replace(stubbed, signature="not-the-right-hmac")

        assert verify_signature(tampered, {}, allow_stub=True) != ""


class TestTheVerifierRunsTheCheck:
    def test_an_unsigned_bundle_is_not_a_match(
        self, suite: BenchSuite, adapter: MockReplayAdapter, bundle: SubmissionBundle
    ) -> None:
        """The state this closes: a bundle with no signature at all passing verification."""
        verifier = BenchVerifier(suite=suite, adapter=adapter)

        result = verifier.verify(bundle)

        assert result.status == VerificationStatus.UNSIGNED
        assert not result.passed

    def test_the_per_task_finding_survives_an_unsigned_headline(
        self, suite: BenchSuite, adapter: MockReplayAdapter, bundle: SubmissionBundle
    ) -> None:
        """The signature decides the headline; it must not swallow the detail.

        "task_a's receipt does not match its hash" is more actionable than "the signature did not
        verify", so the check does not short-circuit.
        """
        verifier = BenchVerifier(suite=suite, adapter=adapter)

        result = verifier.verify(bundle)

        assert [tr.task_id for tr in result.task_results] == ["task_a"]

    def test_an_honest_install_signed_bundle_verifies_end_to_end(
        self, suite: BenchSuite, adapter: MockReplayAdapter, bundle: SubmissionBundle
    ) -> None:
        private_pem, public_pem = _keypair()
        signed = AgentCardSigner(private_pem, public_pem).sign(bundle)
        verifier = BenchVerifier(
            suite=suite,
            adapter=adapter,
            trusted_keys={signed.signer_fingerprint: public_pem},
        )

        assert verifier.verify(signed).passed

    def test_a_caller_can_opt_out_explicitly(
        self, suite: BenchSuite, adapter: MockReplayAdapter, bundle: SubmissionBundle
    ) -> None:
        """Opting out is a decision in the log; omitting the check was not."""
        verifier = BenchVerifier(suite=suite, adapter=adapter, require_signature=False)

        assert verifier.verify(bundle).passed
