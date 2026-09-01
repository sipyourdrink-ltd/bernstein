"""Consent receipt: a donor's DSSE-signed attestation of volunteer-run consent.

A worker's consent to run a task is captured in a signed envelope, mirroring
:mod:`bernstein.core.security.result_receipt_bundle`'s result receipt. The
envelope binds the donor's key, the text of the consent (so it cannot be
rewritten later), and the digests of the policy artifacts the task runs under.

The receipt captures everything a verifier needs to trust the donor consented
without the network:

* the consent text, with its hash;
* the manifest digest and the sandbox profile digest, so the consent is tied
  to the exact policy and containment the task ran under;
* the worker's public key and keyid;
* a timestamp;
* a link into the worker's consent chain (the previous receipt's digest and
  the chain length), so a sequence of receipts is walkable offline.

All of that is placed in an in-toto statement and wrapped in a DSSE envelope
signed by the worker's Ed25519 key, reusing :func:`audit_dsse.pae` and the
envelope dataclasses so the wire format matches the rest of the audit surface.

:func:`verify_consent_receipt` is deliberately offline and side-effect free:

1. the DSSE signature verifies against the donor public key;
2. the embedded consent re-serialises to the subject digest (internal
   consistency);
3. the consent text hashes to its attested value;
4. the chain link is well formed (and matches an expected predecessor when the
   caller walks a sequence);
5. the manifest digest matches the one the caller expects, when it names one --
   the only step that ties the consent to the project's declared policy rather
   than to a policy the donor chose. The verdict records whether that comparison
   happened, because a field carried unchecked is not a field verified.

Tampering with any byte of the consent text or any digest fails verification
with a field-level error, per the result receipt's acceptance criteria.

Every field the statement carries -- predicate, consent, subject, worker, and
chain -- settles its own type before anything reads it, so
:func:`verify_consent_receipt` returns a verdict for every input shape rather
than raising on the malformed ones.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    Signature,
    Statement,
    Subject,
    keyid_from_public_key,
    load_envelope,
    pae,
    parse_envelope,
    verify_envelope,
    write_envelope,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

#: Predicate type for a consent receipt. Distinct from the result-receipt
#: predicate so a verifier cannot confuse the two envelope kinds.
CONSENT_RECEIPT_PREDICATE_TYPE: str = "https://bernstein.run/attestations/consent/v1"

#: Consent receipt schema version, bumped when the field set changes.
CONSENT_SCHEMA_VERSION: str = "1.0.0"

#: Default path for a donor's consent receipt, relative to the project root.
DEFAULT_CONSENT_PATH: str = ".sdd/runtime/volunteer/consent.json"

#: Sentinel anchor for the first consent receipt in a worker's chain.
GENESIS_ANCHOR: str = "genesis"


class ConsentError(RuntimeError):
    """Base class for consent receipt build/verify failures."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sort_recursive(value: Any) -> Any:
    """Reorder dict keys at every depth so canonical JSON is byte-stable."""
    if isinstance(value, dict):
        return {k: _sort_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_recursive(v) for v in value]
    return value


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON: recursively sorted keys, compact separators, UTF-8.

    Matches :func:`audit_dsse._canonical_json`'s discipline so two serialisations
    of the same consent receipt byte-agree -- the property the determinism test
    asserts.
    """
    return json.dumps(_sort_recursive(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# Consent receipt contents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChainLink:
    """A consent receipt's position in the donor's receipt chain.

    ``anchor`` is the previous receipt's digest (or :data:`GENESIS_ANCHOR` for
    the first), ``length`` the count of receipts up to and including this one.
    A verifier walking a sequence checks that each receipt's anchor equals the
    prior receipt's digest -- an offline, key-free continuity check that
    composes with, but does not depend on, the HMAC audit chain.
    """

    anchor: str
    length: int

    def to_dict(self) -> dict[str, Any]:
        return {"anchor": self.anchor, "length": self.length}


@dataclass(frozen=True, slots=True)
class ConsentReceipt:
    """The full contents attested by a consent receipt envelope.

    Attributes:
        consent_text: The full text the donor consented to. Attested as its
            sha256 so tampering is caught at field level.
        manifest_digest: The digest of the project manifest the task runs
            under.
        sandbox_profile_digest: The digest of the sandbox profile the task
            runs behind.
        donor_keyid: The signing key's keyid, derived from
            :func:`audit_dsse.keyid_from_public_key`.
        donor_public_key_pem: The signing key's PEM bytes, exported via
            :func:`audit_dsse.export_public_key_pem`.
        created_at: ISO-8601 UTC timestamp to the second.
        chain: Position in the donor's receipt chain.
        donor_signature: The raw Ed25519 signature bytes, base64-encoded.
    """

    consent_text: str
    manifest_digest: str
    sandbox_profile_digest: str
    donor_keyid: str
    donor_public_key_pem: str
    created_at: str
    chain: ChainLink
    donor_signature: str

    @property
    def consent_text_sha256(self) -> str:
        return _sha256_hex(self.consent_text.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONSENT_SCHEMA_VERSION,
            "consent_text": self.consent_text,
            "consent_text_sha256": self.consent_text_sha256,
            "manifest_digest": self.manifest_digest,
            "sandbox_profile_digest": self.sandbox_profile_digest,
            "donor": {
                "keyid": self.donor_keyid,
                "public_key_pem": self.donor_public_key_pem,
            },
            "created_at": self.created_at,
            "chain": self.chain.to_dict(),
            "signature": self.donor_signature,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        """sha256 of the canonical consent bytes -- the chain anchor successors cite."""
        return _sha256_hex(self.canonical_bytes())


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_consent_receipt(
    receipt: ConsentReceipt,
    *,
    signing_key: Ed25519PrivateKey,
    subject_name: str | None = None,
) -> Envelope:
    """Wrap a :class:`ConsentReceipt` in a signed DSSE envelope.

    Reuses the audit envelope machinery: an in-toto statement whose subject is
    the sha256 of the canonical consent bytes and whose predicate embeds the
    receipt, signed over the DSSE PAE with the donor's Ed25519 key. Ed25519 is
    deterministic, so the same receipt and key produce a byte-identical envelope.
    """
    receipt_dict = receipt.to_dict()
    receipt_bytes = canonical_bytes(receipt_dict)
    digest = _sha256_hex(receipt_bytes)

    subject = Subject(
        name=subject_name or f"consent-{receipt.created_at}.json",
        digest={"sha256": digest},
    )
    predicate = {
        "schema_version": CONSENT_SCHEMA_VERSION,
        "receipt_kind": "consent",
        "receipt": receipt_dict,
        "chain": receipt.chain.to_dict(),
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=CONSENT_RECEIPT_PREDICATE_TYPE,
        predicate=predicate,
    )

    payload = canonical_bytes(statement.to_dict())
    pae_bytes = pae(DSSE_PAYLOAD_TYPE, payload)
    signature = signing_key.sign(pae_bytes)
    keyid = keyid_from_public_key(signing_key.public_key())

    return Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[Signature(keyid=keyid, sig=base64.b64encode(signature).decode("ascii"))],
    )


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConsentVerification:
    """Outcome of :func:`verify_consent_receipt`.

    ``manifest_digest_checked`` is the difference between "the manifest digest
    matched what I expected" and "nobody asked". Without it a caller reading
    ``ok is True`` cannot tell the two apart -- the receipt carries the field
    either way, and an unchecked field reported as verified is the whole defect
    a verifier needs to spot.

    ``profile_digest_checked`` says the same thing about the sandbox profile
    digest, and the same asymmetry applies.

    ``prev_digest_checked`` records whether the chain anchor was actually
    compared, because a malformed chain link short-circuits the comparison.

    The three are recorded independently: a verifier checking all three needs
    all three flags to be True; a verifier checking only the signature and
    receipt shape may intentionally omit the rest, and the verdict makes that
    choice visible.
    """

    ok: bool
    keyid: str = ""
    digest: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    manifest_digest_checked: bool = False
    profile_digest_checked: bool = False
    prev_digest_checked: bool = False


def verify_consent_receipt(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
    *,
    expected_manifest_digest: str | None = None,
    expected_profile_digest: str | None = None,
    expected_prev_digest: str | None = None,
) -> ConsentVerification:
    """Offline, side-effect-free verification of a consent receipt.

    Checks, in order, collecting errors:

    1. DSSE signature verifies against ``public_key`` and the predicate type is
       the consent type (delegated to :func:`audit_dsse.verify_envelope`);
    2. the embedded receipt re-serialises to the attested subject digest;
    3. the signing keyid matches the donor keyid recorded in the receipt;
    4. the consent text hashes to its attested ``consent_text_sha256``;
    5. the chain link is well formed, and matches ``expected_prev_digest`` when
       the caller is walking a sequence;
    6. the manifest digest matches ``expected_manifest_digest`` when the caller
       supplies one;
    7. the sandbox profile digest matches ``expected_profile_digest`` when the
       caller supplies one.

    A single wrong byte in the consent text yields a field-level error naming
    exactly what diverged.

    Steps 6 and 7 tie a consent to the policy and containment a verifier
    expects. Everything above it says the receipt was not altered and that the
    donor signed it; none of it says *which policy* the donor consented to,
    because the donor chose ``manifest_digest`` and ``sandbox_profile_digest``
    too. Passing those arguments is the only step that answers those questions,
    so the result records whether each happened rather than leaving a caller to
    infer it from ``ok``.
    """
    errors: list[str] = []

    env_v = verify_envelope(envelope, public_key, expected_predicate_type=CONSENT_RECEIPT_PREDICATE_TYPE)
    if not env_v.ok:
        return ConsentVerification(
            ok=False,
            keyid=env_v.keyid,
            errors=tuple(env_v.errors),
        )

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return ConsentVerification(
            ok=False,
            errors=("predicate is not a dict",),
        )

    raw_receipt = predicate_dict.get("receipt", {})
    receipt_dict = raw_receipt if isinstance(raw_receipt, dict) else {}
    if not isinstance(raw_receipt, dict):
        errors.append(f"receipt is {type(raw_receipt).__name__}, expected dict")

    raw_subject = statement.get("subject", [])
    attested_digest = ""
    subject_settled = True
    if not isinstance(raw_subject, list):
        subject_settled = False
        errors.append(f"subject is {type(raw_subject).__name__}, expected list")
    elif raw_subject:
        first_subject = raw_subject[0]
        if not isinstance(first_subject, dict):
            subject_settled = False
            errors.append(f"subject[0] is {type(first_subject).__name__}, expected dict")
        elif not isinstance(first_subject.get("digest"), dict):
            subject_settled = False
            errors.append("subject[0] missing digest")
        else:
            attested_digest = first_subject["digest"].get("sha256", "")

    # (2) internal hash consistency: the embedded receipt must reproduce the
    # subject digest byte-for-byte.
    if subject_settled:
        recomputed = _sha256_hex(canonical_bytes(raw_receipt))
        if recomputed != attested_digest:
            errors.append(
                f"embedded receipt hashes to {recomputed}, envelope attests {attested_digest}",
            )

    # (3) the signer is the donor the receipt names.
    donor = receipt_dict.get("donor", {})
    if not isinstance(donor, dict):
        errors.append(f"donor is {type(donor).__name__}, expected dict")
    elif donor.get("keyid") and env_v.keyid and donor["keyid"] != env_v.keyid:
        errors.append(
            f"receipt names donor {donor['keyid']}, signature is by {env_v.keyid}",
        )

    # (4) consent text integrity.
    consent_text = receipt_dict.get("consent_text", "")
    if not isinstance(consent_text, str):
        errors.append(
            f"consent_text is {type(consent_text).__name__}, expected str",
        )
    else:
        attested_hash = receipt_dict.get("consent_text_sha256", "")
        actual_hash = _sha256_hex(consent_text.encode("utf-8"))
        if actual_hash != attested_hash:
            errors.append(
                f"consent text hashes to {actual_hash}, receipt attests {attested_hash}",
            )

    # (5) chain link shape and, optionally, continuity with a predecessor.
    prev_digest_checked = False
    chain = receipt_dict.get("chain", {})
    if not isinstance(chain, dict):
        errors.append(f"chain is {type(chain).__name__}, expected dict")
    elif "anchor" not in chain or "length" not in chain:
        errors.append("chain missing anchor or length")
    elif not isinstance(chain.get("length"), int) or isinstance(chain["length"], bool) or chain["length"] < 1:
        errors.append(f"chain.length invalid: {chain.get('length')!r}")
    elif expected_prev_digest is not None:
        prev_digest_checked = True
        if chain.get("anchor") != expected_prev_digest:
            errors.append(
                f"chain.anchor {chain.get('anchor')} does not link to predecessor {expected_prev_digest}",
            )

    # (6) the manifest digest the consent declares itself bound to. Only
    # compared when the caller names the digest it expects.
    manifest_digest_checked = False
    if expected_manifest_digest is not None:
        manifest_digest_checked = True
        carried = receipt_dict.get("manifest_digest")
        if carried != expected_manifest_digest:
            errors.append(
                f"manifest_digest is {carried!r}, expected {expected_manifest_digest!r}",
            )

    # (7) the sandbox profile digest the consent declares itself bound to.
    # Only compared when the caller names the digest it expects.
    profile_digest_checked = False
    if expected_profile_digest is not None:
        profile_digest_checked = True
        carried = receipt_dict.get("sandbox_profile_digest")
        if carried != expected_profile_digest:
            errors.append(
                f"sandbox_profile_digest is {carried!r}, expected {expected_profile_digest!r}",
            )

    return ConsentVerification(
        ok=not errors,
        keyid=env_v.keyid,
        digest=attested_digest,
        receipt=receipt_dict,
        errors=tuple(errors),
        manifest_digest_checked=manifest_digest_checked,
        profile_digest_checked=profile_digest_checked,
        prev_digest_checked=prev_digest_checked,
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def write_consent(envelope: Envelope, path: Path) -> Path:
    """Persist a consent envelope as canonical JSON (delegates to audit_dsse)."""
    return write_envelope(envelope, path)


def load_consent(path: Path) -> Envelope:
    """Load a consent envelope from disk."""
    return load_envelope(path)


def parse_consent(data: dict[str, Any]) -> Envelope:
    """Parse a consent envelope from an already-decoded dict."""
    return parse_envelope(data)
