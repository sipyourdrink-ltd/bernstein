"""C2PA content credentials as a deterministic projection of the lineage spine.

Issue #2303. Bernstein already writes a signed, Merkle-chained lineage
spine for every artifact (see :mod:`bernstein.core.lineage.spine`). An
operator publishing media or documents produced through Bernstein needs
a *machine-readable* provenance label on those outputs -- and a content
credential is only meaningful if it reflects what actually happened.

This module makes the C2PA manifest a **projection** of the spine rather
than a separately-asserted label:

* The manifest's claim-generator and assertions are populated from the
  artifact's lineage-spine entries. No spine entry for the artifact ->
  no manifest (:class:`ManifestError`); the credential is *unproducible*
  without the chain, not merely unsigned (AC4).
* A **hard binding** assertion (``c2pa.hash.data``) carries the spine
  entry's content hash, so a verifier can bind the manifest to the exact
  bytes the chain recorded (AC1/AC3).
* An **AI actions** assertion (``c2pa.actions``) records the producing
  model and actor drawn from the spine entry (AC1).
* A **soft binding** assertion (``c2pa.soft-binding``) is emitted only
  when a pluggable watermark/fingerprint layer is supplied, so
  multi-layer transparency requirements can be satisfied without
  coupling the projection to any one watermarking scheme.

The manifest is signed with the install-identity Ed25519 key, so one
attestation root covers both "who ran this" (the install identity) and
"what was produced" (the content credential) -- AC5.

Determinism (AC2)
-----------------
:func:`project_manifest` is a pure function of its inputs and never
reads a clock, environment, or socket. The canonical signing bytes are
sorted-key, compact-separator JSON. Ed25519 is deterministic by RFC
8032, so two replays of the same run produce byte-identical manifests
including assertion order and signature bytes.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidSignature

from bernstein.core.lineage.spine import content_hash_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from bernstein.core.lineage.spine import SpineEntry

__all__ = [
    "C2PA_CLAIM_GENERATOR",
    "C2PA_SPEC_VERSION",
    "LABEL_ACTIONS",
    "LABEL_HARD_BINDING",
    "LABEL_SOFT_BINDING",
    "C2paManifest",
    "ManifestError",
    "ManifestIdentity",
    "ManifestVerification",
    "SoftBinding",
    "canonical_manifest_bytes",
    "manifest_from_dict",
    "manifest_to_dict",
    "project_manifest",
    "sign_manifest",
    "verify_manifest",
]

#: C2PA specification version the manifest shape targets.
C2PA_SPEC_VERSION: str = "2.2"

#: Schema version of *this* projection envelope. Bumped on breaking
#: changes to the signed-payload shape.
MANIFEST_SCHEMA_VERSION: str = "1.0.0"

#: Claim generator string identifying Bernstein as the producer. Kept
#: constant so the projection is deterministic across hosts.
C2PA_CLAIM_GENERATOR: str = "bernstein/lineage-c2pa"

#: Standard C2PA assertion labels used by the projection.
LABEL_ACTIONS: str = "c2pa.actions"
LABEL_HARD_BINDING: str = "c2pa.hash.data"
LABEL_SOFT_BINDING: str = "c2pa.soft-binding"

#: IPTC digital-source-type URI for AI-produced media. Recorded in the
#: actions assertion so downstream tools classify the artifact as
#: algorithmically produced.
_DIGITAL_SOURCE_TYPE_AI: str = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


class ManifestError(RuntimeError):
    """Raised when a manifest cannot be projected, signed, or parsed.

    Notably raised by :func:`project_manifest` when there is no lineage
    entry for the requested artifact: the manifest is *unproducible*
    without the spine, not merely unsigned (AC4).
    """


@dataclass(frozen=True, slots=True)
class ManifestIdentity:
    """Install-identity tokens baked into the manifest (AC5).

    Attributes:
        install_rev: Passive install fingerprint from
            :mod:`bernstein.core.identity.install_rev`.
        keyid: Stable id of the signing key (sha256 of the public key
            DER bytes) -- the same key that anchors the install identity.
        run_id: Lineage run id the manifest was projected from.
    """

    install_rev: str = ""
    keyid: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "install_rev": self.install_rev,
            "keyid": self.keyid,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class SoftBinding:
    """A pluggable soft-binding (watermark / fingerprint) layer.

    The projection does not compute watermarks itself; a caller that has
    embedded a watermark or fingerprint supplies its descriptor here and
    the projection surfaces it as a ``c2pa.soft-binding`` assertion. This
    keeps multi-layer transparency pluggable: any watermarking scheme can
    contribute a soft binding without changing the projection.

    Attributes:
        alg: Soft-binding algorithm identifier (e.g. a watermark scheme).
        blocks: Opaque, JSON-serialisable per-scope descriptors. Passed
            through verbatim; the projection never interprets them.
    """

    alg: str
    blocks: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def to_assertion_data(self) -> dict[str, Any]:
        return {"alg": self.alg, "blocks": list(self.blocks)}


@dataclass(slots=True)
class C2paManifest:
    """A C2PA 2.2 manifest projected from the lineage spine.

    The assertion list is order-stable: hard binding first, AI actions
    next, then the optional soft binding. Two projections of the same
    inputs produce byte-identical canonical bytes (AC2).
    """

    schema_version: str
    spec_version: str
    claim_generator: str
    artifact_path: str
    lineage_entry_hash: str
    assertions: list[dict[str, Any]]
    identity: ManifestIdentity
    signature_b64: str = ""

    @property
    def is_signed(self) -> bool:
        return bool(self.signature_b64)

    def hard_binding_hash(self) -> str:
        """Return the content hash pinned by the hard-binding assertion."""
        for assertion in self.assertions:
            if assertion.get("label") == LABEL_HARD_BINDING:
                data = assertion.get("data")
                if isinstance(data, dict):
                    data_dict = cast("dict[str, Any]", data)
                    return str(data_dict.get("hash", ""))
        return ""


@dataclass(frozen=True, slots=True)
class ManifestVerification:
    """Outcome of :func:`verify_manifest`."""

    ok: bool
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def project_manifest(
    *,
    artifact_path: str,
    entries: Sequence[SpineEntry],
    identity: ManifestIdentity,
    soft_binding: SoftBinding | None = None,
) -> C2paManifest:
    """Project the spine entries for ``artifact_path`` into a C2PA manifest.

    Only entries whose ``artifact_path`` matches are considered; the last
    such entry (the most recent write) supplies the content hash and the
    producing model/actor. The manifest pins that entry's hash so it
    links straight back into the chain.

    Args:
        artifact_path: Repo-relative POSIX path of the artifact.
        entries: Spine entries for the run. May include entries for other
            artifacts; the projection filters to ``artifact_path``.
        identity: Install-identity tokens embedded in the manifest.
        soft_binding: Optional pluggable watermark/fingerprint layer. When
            supplied, a ``c2pa.soft-binding`` assertion is appended.

    Returns:
        An unsigned :class:`C2paManifest`. Pass it to :func:`sign_manifest`.

    Raises:
        ManifestError: When no entry matches ``artifact_path``. The
            manifest is unproducible without the spine (AC4).
    """
    matching = [e for e in entries if e.artifact_path == artifact_path]
    if not matching:
        msg = f"no lineage entry for artifact {artifact_path!r}; manifest is unproducible without the spine"
        raise ManifestError(msg)

    source = matching[-1]

    assertions: list[dict[str, Any]] = [
        {
            "label": LABEL_HARD_BINDING,
            "data": {
                "exclusions": [],
                "alg": "sha256",
                "hash": source.content_hash,
            },
        },
        {
            "label": LABEL_ACTIONS,
            "data": {
                "actions": [
                    {
                        "action": "c2pa.created",
                        "softwareAgent": source.model,
                        "digitalSourceType": _DIGITAL_SOURCE_TYPE_AI,
                        "parameters": {
                            "actor": source.actor,
                            "step_id": source.step_id,
                            "lineage_entry_hash": source.entry_hash,
                        },
                    },
                ],
            },
        },
    ]
    if soft_binding is not None:
        assertions.append(
            {
                "label": LABEL_SOFT_BINDING,
                "data": soft_binding.to_assertion_data(),
            }
        )

    return C2paManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        spec_version=C2PA_SPEC_VERSION,
        claim_generator=C2PA_CLAIM_GENERATOR,
        artifact_path=artifact_path,
        lineage_entry_hash=source.entry_hash,
        assertions=assertions,
        identity=identity,
        signature_b64="",
    )


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def _signing_payload_dict(manifest: C2paManifest) -> dict[str, Any]:
    """Return the dict that gets signed (excludes ``signature_b64``)."""
    return {
        "schema_version": manifest.schema_version,
        "spec_version": manifest.spec_version,
        "claim_generator": manifest.claim_generator,
        "artifact_path": manifest.artifact_path,
        "lineage_entry_hash": manifest.lineage_entry_hash,
        "assertions": manifest.assertions,
        "identity": manifest.identity.to_dict(),
    }


def canonical_manifest_bytes(manifest: C2paManifest) -> bytes:
    """Return deterministic signing bytes: sorted keys, compact, UTF-8."""
    return json.dumps(
        _signing_payload_dict(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def manifest_to_dict(manifest: C2paManifest) -> dict[str, Any]:
    """Return the canonical dict view of the manifest (with signature)."""
    payload = _signing_payload_dict(manifest)
    payload["signature_b64"] = manifest.signature_b64
    return payload


def manifest_from_dict(payload: dict[str, Any]) -> C2paManifest:
    """Rebuild a :class:`C2paManifest` from its canonical dict view."""
    identity_raw = payload.get("identity")
    identity_dict: dict[str, Any] = cast("dict[str, Any]", identity_raw) if isinstance(identity_raw, dict) else {}
    identity = ManifestIdentity(
        install_rev=str(identity_dict.get("install_rev", "")),
        keyid=str(identity_dict.get("keyid", "")),
        run_id=str(identity_dict.get("run_id", "")),
    )
    assertions_raw = payload.get("assertions")
    if not isinstance(assertions_raw, list):
        msg = "manifest 'assertions' must be a JSON array"
        raise ManifestError(msg)
    assertions: list[dict[str, Any]] = [
        cast("dict[str, Any]", a) for a in cast("list[Any]", assertions_raw) if isinstance(a, dict)
    ]
    return C2paManifest(
        schema_version=str(payload.get("schema_version", "")),
        spec_version=str(payload.get("spec_version", "")),
        claim_generator=str(payload.get("claim_generator", "")),
        artifact_path=str(payload.get("artifact_path", "")),
        lineage_entry_hash=str(payload.get("lineage_entry_hash", "")),
        assertions=assertions,
        identity=identity,
        signature_b64=str(payload.get("signature_b64", "")),
    )


# ---------------------------------------------------------------------------
# Signing + verification
# ---------------------------------------------------------------------------


def sign_manifest(
    manifest: C2paManifest,
    *,
    signing_key: Ed25519PrivateKey,
) -> C2paManifest:
    """Attach an Ed25519 signature over the canonical manifest bytes (AC5).

    Ed25519 is deterministic (RFC 8032), so signing the same manifest
    twice with the same key yields byte-identical signature bytes (AC2).
    """
    sig = signing_key.sign(canonical_manifest_bytes(manifest))
    return C2paManifest(
        schema_version=manifest.schema_version,
        spec_version=manifest.spec_version,
        claim_generator=manifest.claim_generator,
        artifact_path=manifest.artifact_path,
        lineage_entry_hash=manifest.lineage_entry_hash,
        assertions=manifest.assertions,
        identity=manifest.identity,
        signature_b64=base64.b64encode(sig).decode("ascii"),
    )


def verify_manifest(
    manifest: C2paManifest,
    artifact_content: bytes,
    public_key: Ed25519PublicKey,
) -> ManifestVerification:
    """Verify the manifest against the artifact bytes and a public key.

    Checks, in order:

    * the hard-binding hash equals the sha256 of ``artifact_content``
      (the manifest binds to the exact bytes -- AC3),
    * the manifest carries a signature,
    * the Ed25519 signature verifies against ``public_key`` -- the same
      key that anchors the install identity (AC3/AC5). Any tampering with
      an assertion changes the canonical bytes and fails this check.
    """
    errors: list[str] = []

    expected_hash = content_hash_of(artifact_content)
    if manifest.hard_binding_hash() != expected_hash:
        errors.append(f"hard binding mismatch (expected {expected_hash}, got {manifest.hard_binding_hash()})")

    if not manifest.signature_b64:
        errors.append("manifest is unsigned")
        return ManifestVerification(ok=not errors, errors=tuple(errors))

    try:
        sig = base64.b64decode(manifest.signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        errors.append(f"signature_b64 not valid base64: {exc}")
        return ManifestVerification(ok=False, errors=tuple(errors))

    try:
        public_key.verify(sig, canonical_manifest_bytes(manifest))
    except InvalidSignature:
        errors.append("Ed25519 signature does not verify")

    return ManifestVerification(ok=not errors, errors=tuple(errors))
