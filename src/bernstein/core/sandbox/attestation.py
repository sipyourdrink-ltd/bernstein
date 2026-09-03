"""Signed isolation attestation: what a sandbox backend *delivered* here (#3278).

Every backend in :mod:`bernstein.core.sandbox.backends` advertises its
capabilities as a static class attribute, and ``select_sandbox()`` filters
candidates against those declarations. A declaration is written at authoring
time, on another machine, about a runtime nobody checked on this host, so the
first time the declared set and the delivered set are compared is when
``create()`` raises -- after the scheduler has already placed the task.

An :class:`IsolationAttestation` is the object that closes that gap: a
per-host statement of what each backend was *observed* to deliver, signed with
the install identity (the keypair in
:class:`~bernstein.core.security.agent_card_keystore.AgentCardKeystore`,
published as JWKS at ``/.well-known/agent.json/keys``) so the measurement has a
named signer rather than being an unsourced field.

Three properties make it usable as the selector's input rather than as a log of
the selector's output:

- **The signed body is a pure function of its inputs.** No wall-clock, no run
  id, no chain position -- the same discipline
  :mod:`bernstein.core.sandbox.selection_receipt` follows, and for the same
  reason: re-probing an unchanged host must produce byte-identical bytes, which
  is what makes ``host_facts_digest`` a cache key instead of a timestamp
  heuristic. Lists are canonically ordered before serialisation, because
  ``json.dumps(sort_keys=True)`` sorts dict keys and not list items.
- **Host facts are an allowlist.** A field this module does not know about
  cannot enter the digest, so a collector cannot quietly introduce a varying
  quantity and break byte-identity.
- **Every declared capability carries exactly one verdict**, and the three
  verdict sets are disjoint. A capability that could not be measured stays in
  ``unverifiable`` and there is no code path that moves it into ``observed`` --
  a body claiming both does not construct. That matters most for the remote
  backends, where a client can round-trip ``FILE_RW`` / ``EXEC`` through a
  provider API but cannot observe the provider's isolation boundary at all.

Scope: this module builds and verifies the body. It does not probe -- callers
supply both the host facts and the per-backend measurements, so the probe
runner, the selector wiring and the chain binding land on top of a body whose
shape is already fixed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from bernstein.core.identity.http_signing import install_identity_keyid
from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.security.agent_card_signer import ed25519_public_jwk

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

#: Wire-format version stamped into every attestation body.
ISOLATION_ATTESTATION_SCHEMA_VERSION = 1

#: Discriminator carried in the signed body, so an attestation can never be
#: mistaken for another canonical-JSON receipt signed by the same key.
ATTESTATION_KIND = "sandbox.isolation_attestation"

#: The complete set of top-level keys in the signed body. Exhaustive on
#: purpose: a future field has to be added here, which is where a wall-clock,
#: a run id or a chain position gets caught.
ATTESTATION_BODY_KEYS: frozenset[str] = frozenset(
    {
        "backends",
        "host_facts",
        "host_facts_digest",
        "keyid",
        "kind",
        "public_jwk",
        "schema_version",
    },
)

#: The complete set of keys in one backend entry.
BACKEND_ENTRY_KEYS: frozenset[str] = frozenset(
    {"declared", "name", "observed", "probes", "refuted", "unverifiable"},
)

#: The complete set of keys in one probe entry.
PROBE_ENTRY_KEYS: frozenset[str] = frozenset({"capability", "outcome", "reason_code"})

#: Host facts admitted into the signed body. Anything else is refused at mint
#: time rather than digested, because the digest is only a usable cache key
#: while every input to it is stable across two probes of an unchanged host.
HOST_FACT_KEYS: frozenset[str] = frozenset(
    {
        "arch",
        "cgroup_version",
        "kernel",
        "os",
        "rootless",
        "runtime_binary_digests",
        "runtime_versions",
    },
)


class IsolationAttestationError(ValueError):
    """Raised when an attestation body is malformed or self-contradictory."""


class AttestationVerificationError(IsolationAttestationError):
    """Raised when a signed attestation fails verification.

    Carries a machine-readable :attr:`reason` so a caller can branch on *why*
    verification failed -- a tampered field, an unresolvable signer and a
    rotated identity are different operational events.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ProbeOutcome(StrEnum):
    """Verdict a single capability probe reached on this host.

    Values:
        PASS: The capability was exercised and delivered.
        FAIL: The capability was exercised and did not deliver.
        UNVERIFIABLE: The capability could not be exercised from here at all
            -- the remote backends' isolation strength is the standing case.
            It is never promoted to :attr:`PASS` by any code path.
    """

    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"


def _canonical_json(payload: Any) -> str:
    # allow_nan=False so a non-finite value throws at mint time rather than
    # producing an unparseable "canonical" body.
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public_key_from_jwk(jwk: Mapping[str, str]) -> Ed25519PublicKey:
    """Reconstruct an Ed25519 public key from an OKP JWK (RFC 8037)."""
    x = jwk["x"]
    pad = -len(x) % 4
    raw = base64.urlsafe_b64decode(x + ("=" * pad))
    return Ed25519PublicKey.from_public_bytes(raw)


def _keyid_from_jwk(jwk: Mapping[str, str]) -> str:
    """Return the install-identity key id implied by an embedded JWK.

    Routed through :func:`install_identity_keyid` rather than recomputing the
    RFC 7638 thumbprint here, so the attestation and the HTTP signer can never
    disagree about what a key is called.
    """
    public_pem = _public_key_from_jwk(jwk).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return install_identity_keyid(public_pem)


def _as_list(value: object, label: str) -> list[Any]:
    """Coerce a decoded-JSON member to a list, refusing any other shape."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise IsolationAttestationError(f"attestation member {label!r} must be a list")
    return cast("list[Any]", value)


def _as_object(value: object, label: str) -> dict[str, Any]:
    """Coerce a decoded-JSON member to a string-keyed object, refusing any other shape."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IsolationAttestationError(f"attestation member {label!r} must be an object")
    return {str(k): v for k, v in cast("dict[Any, Any]", value).items()}


def _probe_outcome(value: str | ProbeOutcome) -> ProbeOutcome:
    try:
        return ProbeOutcome(value)
    except ValueError as exc:
        raise IsolationAttestationError(
            f"unknown probe outcome {value!r}; expected one of {sorted(ProbeOutcome)}",
        ) from exc


def _capability(value: str | SandboxCapability) -> SandboxCapability:
    try:
        return SandboxCapability(value)
    except ValueError as exc:
        raise IsolationAttestationError(f"unknown sandbox capability: {value!r}") from exc


def _capability_tuple(values: Iterable[str | SandboxCapability]) -> tuple[SandboxCapability, ...]:
    """Normalise a capability iterable to a de-duplicated, canonically ordered tuple."""
    return tuple(sorted({_capability(v) for v in values}, key=str))


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One capability probe against one backend on this host.

    Attributes:
        capability: The capability the probe exercised.
        outcome: One of :class:`ProbeOutcome`.
        reason_code: Stable machine-readable reason. Required for a ``fail``
            or ``unverifiable`` outcome (an unexplained negative is not
            actionable) and forbidden for a ``pass``.
    """

    capability: SandboxCapability
    outcome: ProbeOutcome
    reason_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _capability(self.capability))
        object.__setattr__(self, "outcome", _probe_outcome(self.outcome))
        if self.outcome == ProbeOutcome.PASS and self.reason_code:
            raise IsolationAttestationError(
                f"probe for {self.capability} passed but carries reason_code {self.reason_code!r}",
            )
        if self.outcome != ProbeOutcome.PASS and not self.reason_code:
            raise IsolationAttestationError(
                f"probe for {self.capability} recorded {self.outcome!r} without a reason_code",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": str(self.capability),
            "outcome": str(self.outcome),
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProbeResult:
        reason = raw.get("reason_code")
        return cls(
            capability=_capability(str(raw.get("capability", ""))),
            outcome=_probe_outcome(str(raw.get("outcome", ""))),
            reason_code=None if reason is None else str(reason),
        )


@dataclass(frozen=True, slots=True)
class BackendAttestation:
    """What one backend declared, and what this host observed of it.

    ``observed``, ``refuted`` and ``unverifiable`` partition ``declared``:
    pairwise disjoint, and together exactly the declared set. A declared
    capability with no verdict would be a third, invisible state that a later
    selector could read either way, so it is refused here.

    ``probes`` need not cover every capability. A backend that raises on
    construction refutes its whole declared set from a single probe, and that
    asymmetry is part of the record.
    """

    name: str
    declared: tuple[SandboxCapability, ...]
    observed: tuple[SandboxCapability, ...] = ()
    refuted: tuple[SandboxCapability, ...] = ()
    unverifiable: tuple[SandboxCapability, ...] = ()
    probes: tuple[ProbeResult, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise IsolationAttestationError("backend attestation requires a non-empty name")
        object.__setattr__(self, "name", self.name.strip())
        for attr in ("declared", "observed", "refuted", "unverifiable"):
            object.__setattr__(self, attr, _capability_tuple(getattr(self, attr)))
        object.__setattr__(
            self,
            "probes",
            tuple(sorted(self.probes, key=lambda p: str(p.capability))),
        )
        self._validate()

    def _validate(self) -> None:
        declared = set(self.declared)
        buckets = {
            "observed": set(self.observed),
            "refuted": set(self.refuted),
            "unverifiable": set(self.unverifiable),
        }
        for label, values in buckets.items():
            undeclared = sorted(str(c) for c in values - declared)
            if undeclared:
                raise IsolationAttestationError(
                    f"backend {self.name!r}: {label} names capabilities that are not declared: {undeclared}",
                )
        names = sorted(buckets)
        for i, left in enumerate(names):
            for right in names[i + 1 :]:
                overlap = sorted(str(c) for c in buckets[left] & buckets[right])
                if overlap:
                    raise IsolationAttestationError(
                        f"backend {self.name!r}: {left} and {right} must be disjoint, both name {overlap}",
                    )
        classified = buckets["observed"] | buckets["refuted"] | buckets["unverifiable"]
        unclassified = sorted(str(c) for c in declared - classified)
        if unclassified:
            raise IsolationAttestationError(
                f"backend {self.name!r}: declared capabilities left unclassified: {unclassified}",
            )

        expected = {
            ProbeOutcome.PASS: buckets["observed"],
            ProbeOutcome.FAIL: buckets["refuted"],
            ProbeOutcome.UNVERIFIABLE: buckets["unverifiable"],
        }
        seen: set[SandboxCapability] = set()
        for probe in self.probes:
            if probe.capability in seen:
                raise IsolationAttestationError(
                    f"backend {self.name!r}: duplicate probe for capability {probe.capability}",
                )
            seen.add(probe.capability)
            if probe.capability not in declared:
                raise IsolationAttestationError(
                    f"backend {self.name!r}: probe for {probe.capability} which is not declared",
                )
            if probe.capability not in expected[probe.outcome]:
                raise IsolationAttestationError(
                    f"backend {self.name!r}: probe outcome {probe.outcome!r} for "
                    f"{probe.capability} contradicts its classification",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared": [str(c) for c in self.declared],
            "observed": [str(c) for c in self.observed],
            "refuted": [str(c) for c in self.refuted],
            "unverifiable": [str(c) for c in self.unverifiable],
            "probes": [p.to_dict() for p in self.probes],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BackendAttestation:
        probes_raw = _as_list(raw.get("probes"), "probes")
        return cls(
            name=str(raw.get("name", "")),
            declared=_capability_tuple(_as_list(raw.get("declared"), "declared")),
            observed=_capability_tuple(_as_list(raw.get("observed"), "observed")),
            refuted=_capability_tuple(_as_list(raw.get("refuted"), "refuted")),
            unverifiable=_capability_tuple(_as_list(raw.get("unverifiable"), "unverifiable")),
            probes=tuple(ProbeResult.from_dict(_as_object(p, "probe")) for p in probes_raw),
        )


@dataclass(frozen=True, slots=True)
class IsolationAttestation:
    """A signed, per-host statement of delivered sandbox isolation.

    The signed body embeds the signer's public JWK so a reviewer can check the
    Ed25519 signature offline, and ``keyid`` is the install-identity thumbprint
    of that key -- a mismatch between the two is itself a verification failure.
    """

    host_facts: dict[str, Any]
    host_facts_digest: str
    backends: tuple[BackendAttestation, ...]
    keyid: str
    public_jwk: dict[str, str]
    schema_version: int = ISOLATION_ATTESTATION_SCHEMA_VERSION
    signature: str = ""

    def __post_init__(self) -> None:
        _validate_host_facts(self.host_facts)
        ordered = tuple(sorted(self.backends, key=lambda b: b.name))
        names = [b.name for b in ordered]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise IsolationAttestationError(f"duplicate backend entries in attestation: {duplicates}")
        object.__setattr__(self, "backends", ordered)

    def signed_body(self) -> dict[str, Any]:
        """The canonical dict that is signed (excludes the signature itself)."""
        return {
            "backends": [b.to_dict() for b in self.backends],
            "host_facts": dict(self.host_facts),
            "host_facts_digest": self.host_facts_digest,
            "keyid": self.keyid,
            "kind": ATTESTATION_KIND,
            "public_jwk": dict(self.public_jwk),
            "schema_version": int(self.schema_version),
        }

    def signing_bytes(self) -> bytes:
        return _canonical_json(self.signed_body()).encode("utf-8")

    def attestation_digest(self) -> str:
        """SHA-256 over the canonical signed body -- the attestation's identity."""
        return _sha256_hex(_canonical_json(self.signed_body()))

    def to_dict(self) -> dict[str, Any]:
        body = self.signed_body()
        body["signature"] = self.signature
        return body

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())


def _validate_host_facts(host_facts: Mapping[str, Any]) -> None:
    unknown = sorted(set(host_facts) - HOST_FACT_KEYS)
    if unknown:
        raise IsolationAttestationError(
            f"unknown host fact key(s) {unknown}; allowed: {sorted(HOST_FACT_KEYS)}",
        )


def host_facts_digest(host_facts: Mapping[str, Any]) -> str:
    """Return the canonical digest of *host_facts* (the re-mint cache key)."""
    _validate_host_facts(host_facts)
    return _sha256_hex(_canonical_json(dict(host_facts)))


def build_isolation_attestation(
    *,
    keystore: AgentCardKeystore,
    host_facts: Mapping[str, Any],
    backends: Iterable[BackendAttestation],
) -> IsolationAttestation:
    """Mint and sign an attestation over caller-supplied measurements.

    Args:
        keystore: The install-identity keystore that signs the body.
        host_facts: Facts about this host, keyed within :data:`HOST_FACT_KEYS`.
        backends: One :class:`BackendAttestation` per measured backend.

    Returns:
        A signed :class:`IsolationAttestation`. Ed25519 signatures are
        deterministic (RFC 8032), so minting twice over identical inputs under
        the same identity yields byte-identical output.

    Raises:
        IsolationAttestationError: When the host facts carry an unknown key,
            a backend entry is self-contradictory, or two entries name the
            same backend.
    """
    _, public_pem = keystore.load_or_generate()
    keyid = install_identity_keyid(public_pem)
    attestation = IsolationAttestation(
        host_facts=dict(host_facts),
        host_facts_digest=host_facts_digest(host_facts),
        backends=tuple(backends),
        keyid=keyid,
        public_jwk=ed25519_public_jwk(public_pem, kid=keyid),
    )
    signature = keystore.signer().sign(attestation.signing_bytes())
    object.__setattr__(attestation, "signature", base64.b64encode(signature).decode("ascii"))
    return attestation


def attestation_from_dict(raw: Mapping[str, Any]) -> IsolationAttestation:
    """Rebuild an attestation from its canonical dict view.

    Raises:
        IsolationAttestationError: When the payload is not a well-formed
            attestation body.
    """
    if str(raw.get("kind", "")) != ATTESTATION_KIND:
        raise IsolationAttestationError(f"payload is not a {ATTESTATION_KIND} body")
    backends_raw = _as_list(raw.get("backends"), "backends")
    jwk_raw = _as_object(raw.get("public_jwk"), "public_jwk")
    return IsolationAttestation(
        host_facts=_as_object(raw.get("host_facts"), "host_facts"),
        host_facts_digest=str(raw.get("host_facts_digest", "")),
        backends=tuple(BackendAttestation.from_dict(_as_object(b, "backend entry")) for b in backends_raw),
        keyid=str(raw.get("keyid", "")),
        public_jwk={k: str(v) for k, v in jwk_raw.items()},
        schema_version=int(raw.get("schema_version", ISOLATION_ATTESTATION_SCHEMA_VERSION)),
        signature=str(raw.get("signature", "")),
    )


def verify_isolation_attestation(
    attestation: IsolationAttestation,
    *,
    key_directory: Mapping[str, Any] | None = None,
) -> None:
    """Verify a signed attestation, raising on the first failure.

    Checks, in order of how much they narrow the failure: the schema version is
    one this build understands; ``keyid`` agrees with the embedded key; the
    embedded ``host_facts_digest`` re-derives from the embedded facts; the
    signer resolves in *key_directory* when one is supplied; and the Ed25519
    signature verifies over the canonical body.

    Args:
        attestation: The attestation to check.
        key_directory: The published JWKS to resolve the signer against. When
            supplied, an attestation signed under a since-rotated install
            identity fails deterministically.

    Raises:
        AttestationVerificationError: On any failure, carrying a ``reason``
            token naming which check failed.
    """
    if attestation.schema_version != ISOLATION_ATTESTATION_SCHEMA_VERSION:
        raise AttestationVerificationError(
            "schema_version_unsupported",
            f"unsupported attestation schema_version {attestation.schema_version!r}",
        )
    if not attestation.signature:
        raise AttestationVerificationError("unsigned", "attestation carries no signature")
    if not attestation.public_jwk:
        raise AttestationVerificationError("missing_public_jwk", "attestation embeds no public key")
    if attestation.keyid != attestation.public_jwk.get("kid"):
        raise AttestationVerificationError(
            "keyid_mismatch",
            "attestation keyid does not match the kid of the embedded public key",
        )
    try:
        derived_keyid = _keyid_from_jwk(attestation.public_jwk)
    except (KeyError, ValueError, TypeError) as exc:
        raise AttestationVerificationError(
            "malformed_public_jwk",
            f"embedded public JWK is not a usable Ed25519 key: {exc}",
        ) from exc
    if derived_keyid != attestation.keyid:
        raise AttestationVerificationError(
            "keyid_mismatch",
            "attestation keyid is not the thumbprint of the embedded public key",
        )

    try:
        expected_digest = host_facts_digest(attestation.host_facts)
    except IsolationAttestationError as exc:
        raise AttestationVerificationError(
            "host_facts_invalid",
            f"attestation host facts are not admissible: {exc}",
        ) from exc
    if expected_digest != attestation.host_facts_digest:
        raise AttestationVerificationError(
            "host_facts_digest_mismatch",
            "host_facts_digest does not re-derive from the embedded host facts",
        )

    if key_directory is not None:
        keys = _as_list(key_directory.get("keys"), "keys")
        known = {str(_as_object(k, "key").get("kid", "")) for k in keys}
        if attestation.keyid not in known:
            raise AttestationVerificationError(
                "keyid_not_in_directory",
                "attestation signer is absent from the supplied key directory",
            )

    try:
        signature = base64.b64decode(attestation.signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise AttestationVerificationError(
            "malformed_signature",
            "attestation signature is not valid base64",
        ) from exc
    try:
        _public_key_from_jwk(attestation.public_jwk).verify(signature, attestation.signing_bytes())
    except (InvalidSignature, KeyError, ValueError, TypeError) as exc:
        raise AttestationVerificationError(
            "signature_invalid",
            "attestation signature does not verify over the canonical body",
        ) from exc


__all__ = [
    "ATTESTATION_BODY_KEYS",
    "ATTESTATION_KIND",
    "BACKEND_ENTRY_KEYS",
    "HOST_FACT_KEYS",
    "ISOLATION_ATTESTATION_SCHEMA_VERSION",
    "PROBE_ENTRY_KEYS",
    "AttestationVerificationError",
    "BackendAttestation",
    "IsolationAttestation",
    "IsolationAttestationError",
    "ProbeOutcome",
    "ProbeResult",
    "attestation_from_dict",
    "build_isolation_attestation",
    "host_facts_digest",
    "verify_isolation_attestation",
]
