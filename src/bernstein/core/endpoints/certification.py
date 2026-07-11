"""Signed endpoint certification receipts (issue #2356).

The result of a conformance run is not a boolean in config -- it is a signed
receipt:

* the conformance transcript and the per-role verdicts are bound into
  canonical JSON and signed with the install's Ed25519 endpoint identity;
* the canonical bytes are anchored in the lineage spine under the dedicated
  ``endpoint-certification`` run, and the anchor is mirrored into the HMAC
  audit chain via ``record_endpoint_certification``;
* config validation gates merge-critical roles on a receipt that still
  verifies -- a hand-edited receipt fails its signature check exactly like a
  tampered chain entry, so "certified" cannot be forged by editing a file.

Determinism: the binding is canonical JSON over the transcript (itself a
pure function of the endpoint's responses), the sorted verdicts, and a
caller-supplied timestamp; Ed25519 is deterministic (RFC 8032), so two runs
that observed the same responses at the same timestamp seal byte-identical
bindings and hashes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.endpoints.conformance import (
    ConformanceTranscript,
    RoleVerdict,
    is_gated_role,
    normalize_base_url,
)
from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

__all__ = [
    "CERTIFICATION_RUN_ID",
    "CERTIFICATION_SCHEMA_VERSION",
    "CertificationVerifyResult",
    "EndpointCertification",
    "build_endpoint_certification",
    "certification_path",
    "certified_roles_for_endpoint",
    "endpoint_fingerprint",
    "load_or_create_endpoint_identity",
    "read_endpoint_certification",
    "validate_endpoint_assignments",
    "verify_endpoint_certification",
]

#: Version stamped into every certification binding preimage. Bump only on
#: a wire-format change.
CERTIFICATION_SCHEMA_VERSION = 1

#: Lineage run id under which every certification is anchored, kept separate
#: so endpoint receipts never interleave with per-task journals.
CERTIFICATION_RUN_ID = "endpoint-certification"

_CERTIFICATION_ACTOR = "bernstein.endpoint_certification"
_CERTIFICATION_SUBPATH = (".sdd", "endpoints", "certifications")
_IDENTITY_PRIVATE_NAME = "endpoint-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "endpoint-identity-public.pem"


def _canonical_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def endpoint_fingerprint(base_url: str, model: str) -> str:
    """Stable hex fingerprint of a normalized ``(base_url, model)`` pair."""
    payload = _canonical_bytes({"base_url": normalize_base_url(base_url), "model": model})
    return hashlib.sha256(payload).hexdigest()


def certification_path(workdir: Path, fingerprint: str) -> Path:
    """Return the on-disk receipt path for *fingerprint* under *workdir*."""
    return workdir.joinpath(*_CERTIFICATION_SUBPATH, f"{fingerprint}.json")


def load_or_create_endpoint_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's endpoint Ed25519 identity.

    The keypair is persisted under *identity_dir* so the same install signs
    every certification and a verifier can check the signature offline
    against the embedded public key. The private key file is written 0600.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    private_path = identity_dir / _IDENTITY_PRIVATE_NAME
    public_path = identity_dir / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
        # Read the raw PEM verbatim -- never strip: the signer key bytes must
        # be byte-identical to what was written or verification fails.
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(".pem.tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Receipt record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointCertification:
    """A sealed endpoint certification receipt.

    ``verdicts`` is a tuple of plain dicts (``role`` / ``certified`` /
    ``reasons``), sorted by role, so the canonical binding is stable and the
    receipt round-trips through JSON without a schema dependency.
    """

    base_url: str
    model: str
    engine: str
    suite_version: int
    transcript: dict[str, Any]
    verdicts: tuple[dict[str, Any], ...]
    timestamp: int
    schema_version: int = CERTIFICATION_SCHEMA_VERSION
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""
    _binding_cache: dict[str, bytes] = field(default_factory=dict, compare=False, repr=False)

    def binding_dict(self) -> dict[str, Any]:
        """The signed portion of the receipt (everything but the seal)."""
        return {
            "schema_version": self.schema_version,
            "base_url": self.base_url,
            "model": self.model,
            "engine": self.engine,
            "suite_version": self.suite_version,
            "transcript": self.transcript,
            "verdicts": list(self.verdicts),
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Canonical JSON bytes of the binding (the signature preimage)."""
        cached = self._binding_cache.get("binding")
        if cached is None:
            cached = _canonical_bytes(self.binding_dict())
            self._binding_cache["binding"] = cached
        return cached

    def certification_hash(self) -> str:
        """``sha256:`` hash of the canonical binding bytes."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    def transcript_hash(self) -> str:
        """Hash of the embedded conformance transcript."""
        return ConformanceTranscript.from_dict(self.transcript).transcript_hash()

    def fingerprint(self) -> str:
        return endpoint_fingerprint(self.base_url, self.model)

    def certified_roles(self) -> frozenset[str]:
        """Roles this receipt certifies (verdicts with ``certified: true``)."""
        return frozenset(str(v["role"]) for v in self.verdicts if v.get("certified"))

    def to_dict(self) -> dict[str, Any]:
        data = self.binding_dict()
        data["signer_public_key_pem"] = self.signer_public_key_pem
        data["signature"] = self.signature
        data["journal_entry_hash"] = self.journal_entry_hash
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EndpointCertification:
        return cls(
            base_url=str(raw["base_url"]),
            model=str(raw["model"]),
            engine=str(raw.get("engine", "")),
            suite_version=int(raw["suite_version"]),
            transcript=dict(raw["transcript"]),
            verdicts=tuple(dict(v) for v in raw["verdicts"]),
            timestamp=int(raw["timestamp"]),
            schema_version=int(raw.get("schema_version", CERTIFICATION_SCHEMA_VERSION)),
            signer_public_key_pem=str(raw.get("signer_public_key_pem", "")),
            signature=str(raw.get("signature", "")),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )


@dataclass(frozen=True)
class CertificationVerifyResult:
    """Outcome of an offline receipt verification."""

    ok: bool
    reason: str
    certification: EndpointCertification | None


# ---------------------------------------------------------------------------
# Build + seal
# ---------------------------------------------------------------------------


def build_endpoint_certification(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    transcript: ConformanceTranscript,
    verdicts: Sequence[RoleVerdict],
    engine: str = "",
    timestamp: int,
    chain: AuditChainStore | None = None,
) -> EndpointCertification:
    """Bind, sign, anchor, and persist one certification receipt.

    The transcript and the sorted verdicts are bound into canonical bytes,
    signed with the endpoint identity, anchored in the certification spine
    run, and written to :func:`certification_path`. When *chain* is supplied
    the seal is mirrored into the HMAC audit chain.

    Returns:
        The sealed :class:`EndpointCertification`.
    """
    unsigned = EndpointCertification(
        base_url=transcript.base_url,
        model=transcript.model,
        engine=engine,
        suite_version=transcript.suite_version,
        transcript=transcript.to_dict(),
        verdicts=tuple(v.to_dict() for v in sorted(verdicts, key=lambda v: v.role)),
        timestamp=timestamp,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)

    fingerprint = unsigned.fingerprint()
    spine = LineageSpine(lineage_root, run_id=CERTIFICATION_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_CERTIFICATION_SUBPATH, f"{fingerprint}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_CERTIFICATION_ACTOR,
        step_id=unsigned.certification_hash(),
        model=transcript.model,
        timestamp=timestamp,
    )

    sealed = EndpointCertification(
        base_url=unsigned.base_url,
        model=unsigned.model,
        engine=unsigned.engine,
        suite_version=unsigned.suite_version,
        transcript=unsigned.transcript,
        verdicts=unsigned.verdicts,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    path = certification_path(workdir, fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_endpoint_certification

        certified = sorted(sealed.certified_roles())
        rejected = sorted(str(v["role"]) for v in sealed.verdicts if not v.get("certified"))
        record_endpoint_certification(
            chain=chain,
            fingerprint=fingerprint,
            model=sealed.model,
            engine=sealed.engine,
            suite_version=sealed.suite_version,
            transcript_hash=sealed.transcript_hash(),
            certified_roles=certified,
            rejected_roles=rejected,
            journal_entry_hash=anchor,
        )
    return sealed


# ---------------------------------------------------------------------------
# Read + verify
# ---------------------------------------------------------------------------


def read_endpoint_certification(workdir: Path, base_url: str, model: str) -> EndpointCertification | None:
    """Return the sealed receipt for ``(base_url, model)`` or ``None``."""
    path = certification_path(workdir, endpoint_fingerprint(base_url, model))
    if not path.is_file():
        return None
    try:
        return EndpointCertification.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("endpoints: malformed certification receipt at %s", path)
        return None


def _signature_ok(certification: EndpointCertification) -> bool:
    outcome = verify_payload(
        certification.to_canonical_bytes(),
        certification.signature or None,
        certification.signer_public_key_pem or None,
        allow_unverified=True,
    )
    return outcome.verified


def verify_endpoint_certification(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    base_url: str,
    model: str,
) -> CertificationVerifyResult:
    """Re-verify the receipt for ``(base_url, model)`` offline.

    Checks, from the stored receipt alone: the identity of the claimed
    endpoint pair, the Ed25519 signature over the canonical binding, and
    the certification spine anchor (the spine itself must verify and
    contain the receipt's entry hash for the same canonical bytes).
    """
    certification = read_endpoint_certification(workdir, base_url, model)
    if certification is None:
        fingerprint = endpoint_fingerprint(base_url, model)
        return CertificationVerifyResult(
            ok=False,
            reason=f"no certification receipt for fingerprint {fingerprint}",
            certification=None,
        )
    if normalize_base_url(base_url) != certification.base_url or model != certification.model:
        return CertificationVerifyResult(
            ok=False,
            reason="receipt endpoint identity does not match the requested endpoint",
            certification=certification,
        )
    if not _signature_ok(certification):
        return CertificationVerifyResult(
            ok=False,
            reason="signature check failed over the canonical binding",
            certification=certification,
        )

    spine = LineageSpine(lineage_root, run_id=CERTIFICATION_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return CertificationVerifyResult(
            ok=False,
            reason=f"certification spine failed verification: {detail}",
            certification=certification,
        )
    expected_content = "sha256:" + hashlib.sha256(certification.to_canonical_bytes()).hexdigest()
    anchored = any(
        entry.entry_hash == certification.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return CertificationVerifyResult(
            ok=False,
            reason="receipt is not anchored in the certification spine",
            certification=certification,
        )
    return CertificationVerifyResult(ok=True, reason="", certification=certification)


def certified_roles_for_endpoint(workdir: Path, base_url: str, model: str) -> frozenset[str]:
    """Return the signature-verified certified roles for an endpoint pair.

    Offline and key-material free: the Ed25519 public key embedded in the
    receipt is enough to detect tampering. An absent, malformed, mismatched,
    or unverifiable receipt certifies nothing (fail closed). Full spine and
    chain verification is available via :func:`verify_endpoint_certification`
    and ``bernstein doctor --endpoint``.
    """
    certification = read_endpoint_certification(workdir, base_url, model)
    if certification is None:
        return frozenset()
    if normalize_base_url(base_url) != certification.base_url or model != certification.model:
        return frozenset()
    if not _signature_ok(certification):
        return frozenset()
    return certification.certified_roles()


# ---------------------------------------------------------------------------
# Config gate (AC3)
# ---------------------------------------------------------------------------


def validate_endpoint_assignments(
    assignments: Sequence[tuple[str, str, str, str]],
    *,
    workdir: Path,
) -> list[str]:
    """Gate ``(role, profile_name, base_url, model)`` assignments on receipts.

    Low-stakes roles (:data:`~bernstein.core.endpoints.conformance.LOCAL_TIER_ROLES`)
    pass without a receipt -- they are best-effort by policy. Every gated
    role requires a stored, signature-verified receipt that certifies that
    exact role for that exact ``(base_url, model)`` pair.

    Returns:
        A list of human-readable error messages; empty when the assignment
        set validates.
    """
    errors: list[str] = []
    for role, profile_name, base_url, model in assignments:
        if not is_gated_role(role):
            continue
        certified = certified_roles_for_endpoint(workdir, base_url, model)
        if role in certified:
            continue
        errors.append(
            f"role_model_policy.{role}: endpoint profile {profile_name!r} ({base_url}) "
            f"has no verified certification for gated role {role!r}. Certify it first: "
            f"bernstein doctor --endpoint {base_url} --endpoint-model {model} --role {role}"
        )
    return errors
