"""
bernstein-bench: bundle signing.

Signs a :class:`SubmissionBundle` off the install identity using Ed25519.
In production this delegates to ``agent_card_signer`` (the same signer used
for agent cards and run receipts).  In tests, a ``StubSigner`` derives a
throwaway signature so signing can be exercised without a real install identity.

The production path FAILS rather than degrades. It used to import a module that
does not exist -- ``bernstein.core.identity.agent_card_signer``, where the real
one is ``bernstein.core.security.agent_card_signer`` -- so its ``except
ImportError`` branch ran on every call and every bundle produced without
``--stub-signer`` was signed by the stub, under a key that is a public constant
in this file. A warning was the only sign (#5856). A missing install identity is
now an error: the stub is reachable only by asking for it.

The signature covers ``bundle_hash`` only — the content hash already commits
to the full task result tree, so signing the hash is equivalent to signing
the entire bundle payload.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle

#: JWS `typ` for a bundle signature. Distinct from the reliability-receipt and agent-card types so
#: a signature made over one artefact kind cannot be replayed as another.
BUNDLE_JWS_TYP = "bernstein-bench-bundle+jws"

# ---------------------------------------------------------------------------
# Signer protocol
# ---------------------------------------------------------------------------


class BundleSignerProtocol(Protocol):
    """Anything that can sign a bundle."""

    def sign(self, bundle: SubmissionBundle) -> SubmissionBundle:
        """
        Return a *new* :class:`SubmissionBundle` with ``signature`` and
        ``signer_fingerprint`` populated.  The original bundle is not mutated.
        """
        ...


# ---------------------------------------------------------------------------
# Stub signer (tests / CI — no real keypair required)
# ---------------------------------------------------------------------------


class StubSigner:
    """
    Deterministic stub: derives a fake Ed25519-like signature from the
    bundle hash using HMAC-SHA256 with a fixed test key.

    Never use in production — the key is public and provides no security.
    """

    _TEST_KEY = b"bernstein-bench-stub-signer-test-key-v1"

    @classmethod
    def fingerprint(cls) -> str:
        """The fingerprint a stub-signed bundle carries.

        Exposed so a verifier can RECOGNISE a stub bundle rather than
        pattern-match the suffix, and refuse it unless the caller opted in.
        """
        return hashlib.sha256(cls._TEST_KEY).hexdigest()[:16] + "-stub"

    @classmethod
    def expected_signature(cls, bundle: SubmissionBundle) -> str:
        """What a stub signature over this bundle must be, for the verifier to compare."""
        import hmac

        raw = hmac.new(cls._TEST_KEY, bundle.bundle_hash().encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def sign(self, bundle: SubmissionBundle) -> SubmissionBundle:
        import hmac

        bundle_hash = bundle.bundle_hash()
        raw_sig = hmac.new(self._TEST_KEY, bundle_hash.encode(), hashlib.sha256).digest()
        signature = base64.b64encode(raw_sig).decode()
        fingerprint = hashlib.sha256(self._TEST_KEY).hexdigest()[:16] + "-stub"

        import dataclasses

        return dataclasses.replace(
            bundle,
            signature=signature,
            signer_fingerprint=fingerprint,
        )


# ---------------------------------------------------------------------------
# Production signer (wraps agent_card_signer)
# ---------------------------------------------------------------------------


class AgentCardSigner:
    """
    Production signer: detached Ed25519 JWS over the bundle hash, keyed by the
    install identity and fingerprinted with the keyid the install publishes.

    The same shape run receipts and reliability receipts use, and deliberately
    the same failure behaviour: signing raises when no key material is
    available. A bundle is never silently downgraded to the stub key -- that
    downgrade is exactly what made `signature` decorative (#5856).

    Explicit key material can be injected for hermetic tests; without it the
    install keystore is used.
    """

    def __init__(
        self,
        private_key_pem: bytes | None = None,
        public_key_pem: bytes | None = None,
    ) -> None:
        if (private_key_pem is None) != (public_key_pem is None):
            raise ValueError("Provide both private_key_pem and public_key_pem, or neither.")
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem

    def _key_material(self) -> tuple[bytes, bytes]:
        if self._private_key_pem is not None and self._public_key_pem is not None:
            return self._private_key_pem, self._public_key_pem
        from bernstein.core.identity.http_signing import default_keystore

        return default_keystore().load_or_generate()

    def fingerprint(self) -> str:
        """The install-identity keyid this signer stamps into bundles."""
        from bernstein.core.identity.http_signing import install_identity_keyid

        _, public_pem = self._key_material()
        return install_identity_keyid(public_pem)

    def public_key_pem(self) -> bytes:
        """SPKI PEM of the verifying key, for building a trusted-key map."""
        _, public_pem = self._key_material()
        return public_pem

    def sign(self, bundle: SubmissionBundle) -> SubmissionBundle:
        import dataclasses

        from bernstein.core.identity.http_signing import install_identity_keyid
        from bernstein.core.security.agent_card_signer import (
            sign_detached_jws_over_canonical,
        )

        private_pem, public_pem = self._key_material()
        kid = install_identity_keyid(public_pem)
        signature = sign_detached_jws_over_canonical(
            bundle.bundle_hash().encode(),
            private_pem,
            typ=BUNDLE_JWS_TYP,
            kid=kid,
        )
        return dataclasses.replace(bundle, signature=signature, signer_fingerprint=kid)
