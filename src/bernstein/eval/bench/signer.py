"""
bernstein-bench: bundle signing.

Signs a :class:`SubmissionBundle` off the install identity using Ed25519.
In production this delegates to ``agent_card_signer`` (the same signer used
for agent cards and run receipts).  In tests, a ``StubSigner`` generates a
throwaway keypair so signing can be exercised without a real install identity.

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
        """The fingerprint every stub-signed bundle carries."""
        return hashlib.sha256(cls._TEST_KEY).hexdigest()[:16] + "-stub"

    @classmethod
    def expected_signature(cls, bundle: SubmissionBundle) -> str:
        """The signature the stub would produce for *bundle*'s current hash."""
        import hmac

        raw_sig = hmac.new(cls._TEST_KEY, bundle.bundle_hash().encode(), hashlib.sha256).digest()
        return base64.b64encode(raw_sig).decode()

    @classmethod
    def verify(cls, bundle: SubmissionBundle) -> bool:
        """True when *bundle* carries the stub's fingerprint and a matching signature.

        The stub key is public, so this proves only that the signature was
        produced over *this* hash -- a bundle re-hashed after signing fails
        it -- not that anyone in particular signed it.
        """
        import hmac

        return bundle.signer_fingerprint == cls.fingerprint() and hmac.compare_digest(
            bundle.signature, cls.expected_signature(bundle)
        )

    def sign(self, bundle: SubmissionBundle) -> SubmissionBundle:
        signature = self.expected_signature(bundle)
        fingerprint = self.fingerprint()

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
    Production signer: delegates to ``bernstein.core.identity.agent_card_signer``.

    Falls back to :class:`StubSigner` with a warning if the identity module
    is not available (e.g. during a fresh clone before ``bernstein init``).
    """

    def sign(self, bundle: SubmissionBundle) -> SubmissionBundle:
        try:
            from bernstein.core.identity.agent_card_signer import sign_payload  # type: ignore[import]

            bundle_hash = bundle.bundle_hash()
            sig_result = sign_payload(bundle_hash.encode())
            import dataclasses

            return dataclasses.replace(
                bundle,
                signature=sig_result["signature"],
                signer_fingerprint=sig_result["fingerprint"],
            )
        except ImportError:
            import warnings

            warnings.warn(
                "agent_card_signer not available — falling back to StubSigner. "
                "Run `bernstein init` to set up your install identity.",
                stacklevel=2,
            )
            return StubSigner().sign(bundle)
