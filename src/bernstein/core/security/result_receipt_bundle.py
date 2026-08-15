"""Result receipt bundle: the portable, offline-verifiable unit of trust (#3870).

A worker's submission exists only as far as its bundle verifies. The primitives
already ship -- Ed25519 receipts, the DSSE / in-toto envelope in
:mod:`bernstein.core.security.audit_dsse`, the hash chain -- and this module
shapes them into one artifact and one offline check, without reinventing the
crypto.

The bundle captures everything a verifier needs to trust a result without the
network:

* the patch, with its sha256;
* each gate command, its exit code, and a digest of its log (log text embedded
  so tampering is caught at field level);
* the task reference (repo, commit sha, issue);
* a manifest hash, adapter and model identifiers;
* the sandbox profile and its selection receipt;
* timestamps and the worker's public key;
* a link into the worker's receipt chain (the previous bundle's digest and the
  chain length), so a sequence of bundles is walkable offline.

All of that is placed in an in-toto statement and wrapped in a DSSE envelope
signed by the worker's Ed25519 key, reusing :func:`audit_dsse.pae` and the
envelope dataclasses so the wire format matches the rest of the audit surface.

:func:`verify_result_bundle` is deliberately offline and side-effect free:

1. the DSSE signature verifies against the worker public key;
2. the embedded bundle re-serialises to the subject digest (internal
   consistency);
3. the patch hashes to its attested value;
4. every gate log hashes to its attested digest;
5. the chain link is well formed (and matches an expected predecessor when the
   caller walks a sequence).

Tampering with any byte of the patch or any gate log fails verification with a
field-level error, per the issue's acceptance criteria. HMAC audit-chain
verification stays with whoever holds the chain key, exactly as
:func:`audit_dsse.verify_envelope` documents; this bundle composes with it but
does not require it.
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

#: Predicate type for a result receipt bundle. Distinct from the audit
#: predicate so a verifier cannot confuse the two envelope kinds.
RESULT_RECEIPT_PREDICATE_TYPE: str = "https://bernstein.run/attestations/result-receipt/v1"

#: Bundle schema version, bumped when the field set changes.
BUNDLE_SCHEMA_VERSION: str = "1.0.0"

#: Sentinel anchor for the first bundle in a worker's chain.
GENESIS_ANCHOR: str = "genesis"


class BundleError(RuntimeError):
    """Base class for bundle build/verify failures."""


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
    of the same bundle byte-agree -- the property the determinism test asserts.
    """
    return json.dumps(_sort_recursive(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# Bundle contents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate command, its exit code, and its log.

    The log text is embedded and its digest attested, so tampering with a single
    byte of a gate's output is a field-level verification failure rather than a
    silent divergence.
    """

    command: str
    exit_code: int
    log: str

    @property
    def log_sha256(self) -> str:
        return _sha256_hex(self.log.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "log": self.log,
            "log_sha256": self.log_sha256,
        }


@dataclass(frozen=True, slots=True)
class TaskRef:
    repo: str
    commit_sha: str
    issue_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "commit_sha": self.commit_sha, "issue_number": self.issue_number}


@dataclass(frozen=True, slots=True)
class ChainLink:
    """A bundle's position in the worker's receipt chain.

    ``anchor`` is the previous bundle's digest (or :data:`GENESIS_ANCHOR` for the
    first), ``length`` the count of bundles up to and including this one. A
    verifier walking a sequence checks that each bundle's anchor equals the prior
    bundle's digest -- an offline, key-free continuity check that composes with,
    but does not depend on, the HMAC audit chain.
    """

    anchor: str
    length: int

    def to_dict(self) -> dict[str, Any]:
        return {"anchor": self.anchor, "length": self.length}


@dataclass(frozen=True, slots=True)
class ResultBundle:
    """The full contents attested by a result receipt envelope."""

    task: TaskRef
    patch: str
    gates: tuple[GateResult, ...]
    manifest_sha256: str
    adapter_id: str
    model_id: str
    sandbox_profile: str
    selection_receipt: str
    created_at: str
    worker_keyid: str
    worker_public_key_pem: str
    chain: ChainLink

    @property
    def patch_sha256(self) -> str:
        return _sha256_hex(self.patch.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "task": self.task.to_dict(),
            "patch": self.patch,
            "patch_sha256": self.patch_sha256,
            "gates": [g.to_dict() for g in self.gates],
            "manifest_sha256": self.manifest_sha256,
            "adapter_id": self.adapter_id,
            "model_id": self.model_id,
            "sandbox": {
                "profile": self.sandbox_profile,
                "selection_receipt": self.selection_receipt,
            },
            "created_at": self.created_at,
            "worker": {
                "keyid": self.worker_keyid,
                "public_key_pem": self.worker_public_key_pem,
            },
            "chain": self.chain.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        """sha256 of the canonical bundle bytes -- the chain anchor successors cite."""
        return _sha256_hex(self.canonical_bytes())


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_result_bundle(
    bundle: ResultBundle,
    *,
    signing_key: Ed25519PrivateKey,
    subject_name: str | None = None,
) -> Envelope:
    """Wrap a :class:`ResultBundle` in a signed DSSE envelope.

    Reuses the audit envelope machinery: an in-toto statement whose subject is
    the sha256 of the canonical bundle bytes and whose predicate embeds the
    bundle, signed over the DSSE PAE with the worker's Ed25519 key. Ed25519 is
    deterministic, so the same bundle and key produce a byte-identical envelope.
    """
    bundle_dict = bundle.to_dict()
    bundle_bytes = canonical_bytes(bundle_dict)
    digest = _sha256_hex(bundle_bytes)

    subject = Subject(
        name=subject_name or f"result-receipt-{bundle.task.commit_sha[:12]}.json",
        digest={"sha256": digest},
    )
    predicate = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_kind": "result-receipt",
        "bundle": bundle_dict,
        "chain": bundle.chain.to_dict(),
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=RESULT_RECEIPT_PREDICATE_TYPE,
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
class FieldError:
    """A field-level verification failure -- which field, and why."""

    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.field}: {self.message}"


@dataclass(frozen=True, slots=True)
class BundleVerification:
    """Outcome of :func:`verify_result_bundle`."""

    ok: bool
    keyid: str = ""
    digest: str = ""
    bundle: dict[str, Any] = field(default_factory=dict)
    errors: tuple[FieldError, ...] = ()


def verify_result_bundle(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
    *,
    expected_prev_digest: str | None = None,
) -> BundleVerification:
    """Offline, side-effect-free verification of a result receipt bundle.

    Checks, in order, collecting field-level errors:

    1. DSSE signature verifies against ``public_key`` and the predicate type is
       the result-receipt type (delegated to :func:`audit_dsse.verify_envelope`);
    2. the embedded bundle re-serialises to the attested subject digest;
    3. the signing keyid matches the worker keyid recorded in the bundle;
    4. the patch hashes to its attested ``patch_sha256``;
    5. every gate log hashes to its attested ``log_sha256``;
    6. the chain link is well formed, and matches ``expected_prev_digest`` when
       the caller is walking a sequence.

    A single wrong byte in the patch or any gate log yields a field-level error
    naming exactly what diverged.
    """
    errors: list[FieldError] = []

    env_v = verify_envelope(envelope, public_key, expected_predicate_type=RESULT_RECEIPT_PREDICATE_TYPE)
    if not env_v.ok:
        # signature / envelope-shape failure: nothing below can be trusted.
        return BundleVerification(
            ok=False,
            keyid=env_v.keyid,
            errors=tuple(FieldError("envelope", e) for e in env_v.errors),
        )

    statement = env_v.statement
    predicate = statement.get("predicate", {})
    bundle_dict = predicate.get("bundle", {})
    subjects = statement.get("subject", [])
    attested_digest = ""
    if subjects and isinstance(subjects[0], dict):
        attested_digest = subjects[0].get("digest", {}).get("sha256", "")

    # (2) internal hash consistency: the embedded bundle must reproduce the
    # subject digest byte-for-byte.
    recomputed = _sha256_hex(canonical_bytes(bundle_dict))
    if recomputed != attested_digest:
        errors.append(
            FieldError(
                "subject.digest.sha256",
                f"embedded bundle hashes to {recomputed}, envelope attests {attested_digest}",
            )
        )

    # (3) the signer is the worker the bundle names.
    worker = bundle_dict.get("worker", {})
    if worker.get("keyid") and env_v.keyid and worker["keyid"] != env_v.keyid:
        errors.append(
            FieldError(
                "worker.keyid",
                f"bundle names worker {worker['keyid']}, signature is by {env_v.keyid}",
            )
        )

    # (4) patch integrity.
    patch = bundle_dict.get("patch", "")
    attested_patch = bundle_dict.get("patch_sha256", "")
    actual_patch = _sha256_hex(patch.encode("utf-8"))
    if actual_patch != attested_patch:
        errors.append(
            FieldError(
                "patch",
                f"patch hashes to {actual_patch}, bundle attests {attested_patch}",
            )
        )

    # (5) gate-log integrity, per gate.
    for index, gate in enumerate(bundle_dict.get("gates", [])):
        log = gate.get("log", "")
        attested_log = gate.get("log_sha256", "")
        actual_log = _sha256_hex(log.encode("utf-8"))
        if actual_log != attested_log:
            errors.append(
                FieldError(
                    f"gates[{index}].log",
                    f"log for {gate.get('command', '?')!r} hashes to {actual_log}, bundle attests {attested_log}",
                )
            )

    # (6) chain link shape and, optionally, continuity with a predecessor.
    chain = bundle_dict.get("chain", {})
    if "anchor" not in chain or "length" not in chain:
        errors.append(FieldError("chain", "missing anchor or length"))
    elif not isinstance(chain.get("length"), int) or chain["length"] < 1:
        errors.append(FieldError("chain.length", f"invalid length {chain.get('length')!r}"))
    elif expected_prev_digest is not None and chain.get("anchor") != expected_prev_digest:
        errors.append(
            FieldError(
                "chain.anchor",
                f"anchor {chain.get('anchor')} does not link to predecessor {expected_prev_digest}",
            )
        )

    return BundleVerification(
        ok=not errors,
        keyid=env_v.keyid,
        digest=attested_digest,
        bundle=bundle_dict,
        errors=tuple(errors),
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def write_bundle(envelope: Envelope, path: Path) -> Path:
    """Persist a bundle envelope as canonical JSON (delegates to audit_dsse)."""
    return write_envelope(envelope, path)


def load_bundle(path: Path) -> Envelope:
    """Load a bundle envelope from disk."""
    return load_envelope(path)


def parse_bundle(data: dict[str, Any]) -> Envelope:
    """Parse a bundle envelope from an already-decoded dict."""
    return parse_envelope(data)
