"""Sovereign deployment profile: one posture, one signed identity (issue #2518).

Self-hosted operators under data-residency constraints today assemble their
posture by hand from four independent surfaces (the air-gap network profile,
residency policies, endpoint certification, and storage / catalog / compliance
defaults). Each piece works; the composition is manual, and a single missed
setting -- a cloud artifact sink left configured, a non-certified remote
endpoint in a role, a catalog left online -- violates the operator's
constraints silently, because nothing states the intended posture and so
nothing can detect drift from it.

This module turns the active posture into a **signed, verifiable claim**:

1. Profile resolution is a *pure function* ``(profile_name, config_snapshot)
   -> EffectivePolicy``. The effective policy pins deny-all egress, offline
   catalog mode, local storage backends, strict EU residency, and the
   compliance pack; it also projects the config-derived residency-relevant
   keys (declared endpoints, storage backend, catalogs, residency regions) so
   a config edit changes the document. The document's canonical-JSON SHA-256
   is the **posture identity**. Determinism: the same config snapshot yields
   byte-identical canonical bytes -> byte-identical hash on any host, so an
   auditor recomputes the identity from the config snapshot alone (an ordinary
   settings preset has no independently recomputable posture identity).

2. Posture attestation: on activation the effective-policy document is signed
   with the install's Ed25519 sovereign identity
   (:mod:`bernstein.core.lineage.identity`) and anchored in the HMAC audit
   chain via ``record_sovereign_attestation``. The attestation -- not the
   config file -- is what an auditor checks; the embedded public key makes the
   signature key-material-free to verify offline.

3. Drift refusal: at spawn time the orchestrator recomputes the effective
   posture from the live config and compares its hash to the attested hash;
   any divergence blocks the spawn and writes a *signed* drift record naming
   the diverging keys. The drift record re-verifies as part of ``bernstein
   audit verify``.

The value of the feature is inseparable from the substrate: strip the signed
identity and the HMAC audit chain and the "attestation" is just a hash in a
file that anyone can rewrite. Here the attestation is the audit chain in the
shape of a residency posture -- a signed receipt an auditor checks against the
chain instead of interviewing the operator.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "EFFECTIVE_POLICY_SCHEMA_VERSION",
    "LOCAL_STORAGE_BACKENDS",
    "SOVEREIGN_EU_REGIONS",
    "SOVEREIGN_PROFILE",
    "DriftEvaluation",
    "EffectivePolicy",
    "PostureAttestation",
    "PostureDriftRefusal",
    "SovereignVerifyResult",
    "attestation_path",
    "build_posture_attestation",
    "evaluate_posture_drift",
    "is_local_or_eu_host",
    "load_config_snapshot",
    "load_or_create_sovereign_identity",
    "read_posture_attestation",
    "record_and_sign_drift",
    "resolve_effective_policy",
    "verify_sovereign_attestations",
]

#: Name of the composed profile, activated with ``--profile sovereign``.
SOVEREIGN_PROFILE = "sovereign"

#: Version stamped into every effective-policy document and signed body. Bump
#: only on a wire-format change; the verifier rejects unknown versions.
EFFECTIVE_POLICY_SCHEMA_VERSION = 1

#: EU residency regions the sovereign profile pins by default. Mirrors the
#: EU-region members of :class:`bernstein.core.security.data_residency.Region`.
SOVEREIGN_EU_REGIONS: frozenset[str] = frozenset({"eu-west", "eu-central"})

#: Storage backends that keep artifacts on the operator's own disk. ``memory``
#: is the only shipped backend with no external sink; ``postgres`` / ``redis``
#: point at a network service and are rejected as non-local sinks.
LOCAL_STORAGE_BACKENDS: frozenset[str] = frozenset({"memory"})

#: The pinned constants the sovereign profile forces regardless of config.
_PINNED_NETWORK_EGRESS = "deny-all"
_PINNED_CATALOG_MODE = "offline"
_PINNED_COMPLIANCE_PACK = "regulated"

_ATTESTATION_SUBDIR = (".sdd", "sovereign")
_ATTESTATION_NAME = "attestation.json"
_IDENTITY_PRIVATE_NAME = "sovereign-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "sovereign-identity-public.pem"

_RECORD_KIND_ATTESTATION = "sovereign_attestation"
_RECORD_KIND_DRIFT = "sovereign_drift"


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(data: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_of(data: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(data)).hexdigest()


def is_local_or_eu_host(base_url: str) -> bool:
    """Return True iff *base_url*'s host is loopback, RFC-1918, or cluster-local.

    Mirrors the self-hosted allowlist documented for the EU-residency profile
    (loopback, RFC-1918 private ranges, ``*.internal`` / ``*.local`` / ``*.svc``
    / ``*.cluster.local``). A public IP or a hosted-API FQDN returns False. An
    empty / unparseable URL returns False (fail closed).
    """
    if not base_url:
        return False
    host = urlparse(base_url if "://" in base_url else f"//{base_url}").hostname or ""
    host = host.strip().lower()
    if not host:
        return False
    if host in {"localhost", "0.0.0.0"}:
        return True
    if host.endswith((".internal", ".local", ".svc", ".cluster.local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


# ---------------------------------------------------------------------------
# Effective policy (the posture identity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The canonical effective-policy projection for a deployment profile.

    Produced by :func:`resolve_effective_policy` as a pure function of
    ``(profile_name, config_snapshot)``. The pinned fields are profile
    constants; the remaining fields are projected from the config snapshot so
    an edit (for example adding a cloud storage sink) changes the document and
    therefore the posture hash. :meth:`posture_hash` is the posture identity.
    """

    profile: str
    schema_version: int
    network_egress: str
    catalog_mode: str
    compliance_pack: str
    storage_backend: str
    residency_enforce_strict: bool
    residency_regions: tuple[str, ...]
    model_endpoints: tuple[dict[str, Any], ...]
    catalogs: tuple[dict[str, Any], ...]

    def to_canonical_document(self) -> dict[str, Any]:
        """Return the canonical, JSON-serialisable posture document."""
        return {
            "profile": self.profile,
            "schema_version": self.schema_version,
            "network_egress": self.network_egress,
            "catalog_mode": self.catalog_mode,
            "compliance_pack": self.compliance_pack,
            "storage_backend": self.storage_backend,
            "residency_enforce_strict": self.residency_enforce_strict,
            "residency_regions": list(self.residency_regions),
            "model_endpoints": [dict(e) for e in self.model_endpoints],
            "catalogs": [dict(c) for c in self.catalogs],
        }

    def posture_hash(self) -> str:
        """``sha256:`` hash of the canonical posture document (the identity)."""
        return _sha256_of(self.to_canonical_document())

    def violations(self) -> list[str]:
        """Return config-only reasons the posture is non-compliant.

        Covers everything checkable from the config snapshot alone: storage
        backend locality, catalog offline mode, strict EU residency, and
        endpoint host locality. Endpoint *certification* (which requires the
        on-disk receipts) is checked separately by
        :func:`endpoint_certification_violations`.
        """
        problems: list[str] = []
        if self.network_egress != _PINNED_NETWORK_EGRESS:
            problems.append(f"network egress is {self.network_egress!r}, sovereign requires deny-all")
        if self.catalog_mode != _PINNED_CATALOG_MODE:
            problems.append(f"catalog mode is {self.catalog_mode!r}, sovereign requires offline")
        if self.storage_backend not in LOCAL_STORAGE_BACKENDS:
            problems.append(
                f"storage.backend {self.storage_backend!r} is not a local backend "
                f"(allowed: {sorted(LOCAL_STORAGE_BACKENDS)})"
            )
        if not self.residency_enforce_strict:
            problems.append("residency enforcement is not strict (enforce_strict must be true)")
        outside = sorted(r for r in self.residency_regions if r not in SOVEREIGN_EU_REGIONS)
        if outside:
            problems.append(f"residency regions {outside} are outside the EU set {sorted(SOVEREIGN_EU_REGIONS)}")
        if not self.residency_regions:
            problems.append("no residency regions pinned; sovereign requires at least one EU region")
        for entry in self.catalogs:
            if entry.get("enabled"):
                problems.append(f"catalog {entry.get('name')!r} is enabled; sovereign requires offline catalog mode")
        for endpoint in self.model_endpoints:
            base_url = str(endpoint.get("base_url") or "")
            if base_url and not is_local_or_eu_host(base_url):
                problems.append(
                    f"role {endpoint.get('role')!r} endpoint {base_url!r} is neither certified-local nor EU-region"
                )
        return problems


def _project_endpoints(config: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Project the declared per-role model endpoints from the config snapshot.

    Resolves ``role_model_policy.<role>.endpoint`` references against
    ``local_endpoints`` so the projection is complete from the raw config,
    without depending on pydantic validation having materialised them.
    """
    policy = config.get("role_model_policy")
    if not isinstance(policy, Mapping):
        return ()
    profiles_raw = config.get("local_endpoints")
    profiles: Mapping[str, Any] = profiles_raw if isinstance(profiles_raw, Mapping) else {}
    projected: list[dict[str, Any]] = []
    for role, entry in policy.items():
        if not isinstance(entry, Mapping):
            continue
        base_url = entry.get("base_url")
        model = entry.get("model")
        profile_name = entry.get("endpoint")
        if profile_name is not None:
            profile = profiles.get(profile_name)
            if isinstance(profile, Mapping):
                base_url = profile.get("base_url", base_url)
                model = profile.get("model", model)
        if base_url is None and model is None and profile_name is None:
            # A role that only pins cli/effort with no endpoint is not
            # residency-relevant; skip it so the projection stays stable.
            continue
        projected.append(
            {
                "role": str(role),
                "endpoint": str(profile_name) if profile_name is not None else "",
                "base_url": str(base_url) if base_url is not None else "",
                "model": str(model) if model is not None else "",
            }
        )
    projected.sort(key=lambda e: (e["role"], e["base_url"], e["model"]))
    return tuple(projected)


def _project_catalogs(config: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Project the declared catalog sources (name + enabled) from the config."""
    catalogs = config.get("catalogs")
    if not isinstance(catalogs, Sequence) or isinstance(catalogs, (str, bytes)):
        return ()
    projected: list[dict[str, Any]] = []
    for item in catalogs:
        if not isinstance(item, Mapping):
            continue
        projected.append({"name": str(item.get("name", "")), "enabled": bool(item.get("enabled", True))})
    projected.sort(key=lambda c: c["name"])
    return tuple(projected)


def _project_residency(config: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Project (enforce_strict, regions) from the ``sovereign`` config block."""
    block = config.get("sovereign")
    if not isinstance(block, Mapping):
        return True, tuple(sorted(SOVEREIGN_EU_REGIONS))
    enforce_strict = bool(block.get("enforce_strict", True))
    regions_raw = block.get("regions")
    if isinstance(regions_raw, Sequence) and not isinstance(regions_raw, (str, bytes)):
        regions = tuple(sorted(str(r) for r in regions_raw))
    else:
        regions = tuple(sorted(SOVEREIGN_EU_REGIONS))
    return enforce_strict, regions


def resolve_effective_policy(profile_name: str, config_snapshot: Mapping[str, Any] | None) -> EffectivePolicy:
    """Resolve the canonical effective policy for *profile_name* (pure function).

    Args:
        profile_name: The deployment profile name (``sovereign``).
        config_snapshot: The parsed ``bernstein.yaml`` mapping, or ``None`` for
            an empty config. Only residency-relevant keys are read; the
            function performs no I/O and no env expansion, so an auditor holding
            only the config file recomputes the identical document and hash.

    Returns:
        The :class:`EffectivePolicy` whose :meth:`~EffectivePolicy.posture_hash`
        is the posture identity.
    """
    config: Mapping[str, Any] = config_snapshot or {}
    storage = config.get("storage")
    storage_backend = "memory"
    if isinstance(storage, Mapping):
        storage_backend = str(storage.get("backend", "memory"))
    enforce_strict, regions = _project_residency(config)
    return EffectivePolicy(
        profile=profile_name,
        schema_version=EFFECTIVE_POLICY_SCHEMA_VERSION,
        network_egress=_PINNED_NETWORK_EGRESS,
        catalog_mode=_PINNED_CATALOG_MODE,
        compliance_pack=_PINNED_COMPLIANCE_PACK,
        storage_backend=storage_backend,
        residency_enforce_strict=enforce_strict,
        residency_regions=regions,
        model_endpoints=_project_endpoints(config),
        catalogs=_project_catalogs(config),
    )


def endpoint_certification_violations(policy: EffectivePolicy, *, workdir: Path) -> list[str]:
    """Return gated endpoints lacking a verified certification receipt (AC4).

    Reuses the signed endpoint-certification receipts
    (:mod:`bernstein.core.endpoints.certification`). A gated role routed to a
    remote endpoint with no verified receipt for that exact ``(base_url,
    model)`` pair is a violation -- the posture refuses to route to a
    non-certified endpoint rather than silently downgrading.
    """
    from bernstein.core.endpoints.certification import certified_roles_for_endpoint
    from bernstein.core.endpoints.conformance import is_gated_role

    problems: list[str] = []
    for endpoint in policy.model_endpoints:
        role = str(endpoint.get("role") or "")
        base_url = str(endpoint.get("base_url") or "")
        model = str(endpoint.get("model") or "")
        if not base_url or not is_gated_role(role):
            continue
        certified = certified_roles_for_endpoint(workdir, base_url, model)
        if role not in certified:
            problems.append(f"role {role!r} routes to {base_url!r} ({model}) with no verified certification receipt")
    return problems


# ---------------------------------------------------------------------------
# Config snapshot loading
# ---------------------------------------------------------------------------


def load_config_snapshot(workdir: Path) -> dict[str, Any]:
    """Return the raw parsed ``bernstein.yaml`` mapping under *workdir*.

    Reads the file verbatim (no env expansion, no pydantic validation) so the
    projection matches exactly what an auditor recomputes from the same file.
    A missing or non-mapping config yields ``{}``.
    """
    import yaml

    path = workdir / "bernstein.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Identity + attestation record
# ---------------------------------------------------------------------------


def attestation_path(workdir: Path) -> Path:
    """Return the on-disk attestation receipt path under *workdir*."""
    return workdir.joinpath(*_ATTESTATION_SUBDIR, _ATTESTATION_NAME)


def load_or_create_sovereign_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's sovereign Ed25519 identity.

    Reuses the persisted-identity primitive from
    :mod:`bernstein.core.lineage.identity`; the private key is written ``0600``
    and the public key is embedded in every attestation so verification is
    offline and key-material free.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    from bernstein.core.lineage.identity import load_or_create_signing_identity

    return load_or_create_signing_identity(
        identity_dir,
        private_name=_IDENTITY_PRIVATE_NAME,
        public_name=_IDENTITY_PUBLIC_NAME,
    )


@dataclass(frozen=True, slots=True)
class PostureAttestation:
    """A signed posture attestation: the effective policy plus its Ed25519 seal."""

    profile: str
    schema_version: int
    posture_hash: str
    effective_policy: dict[str, Any]
    timestamp: int
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""

    def signed_body(self) -> dict[str, Any]:
        """The signed preimage (everything but the seal fields)."""
        return {
            "record_kind": _RECORD_KIND_ATTESTATION,
            "schema_version": self.schema_version,
            "profile": self.profile,
            "posture_hash": self.posture_hash,
            "effective_policy": self.effective_policy,
            "timestamp": self.timestamp,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.signed_body()
        data["signer_public_key_pem"] = self.signer_public_key_pem
        data["signature"] = self.signature
        data["journal_entry_hash"] = self.journal_entry_hash
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PostureAttestation:
        return cls(
            profile=str(raw["profile"]),
            schema_version=int(raw["schema_version"]),
            posture_hash=str(raw["posture_hash"]),
            effective_policy=dict(raw["effective_policy"]),
            timestamp=int(raw["timestamp"]),
            signer_public_key_pem=str(raw.get("signer_public_key_pem", "")),
            signature=str(raw.get("signature", "")),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )


def build_posture_attestation(
    *,
    workdir: Path,
    policy: EffectivePolicy,
    timestamp: int,
    chain: AuditChainStore | None = None,
) -> PostureAttestation:
    """Sign, persist, and (optionally) anchor a posture attestation.

    The effective-policy document is bound into a canonical signed body,
    signed with the install's sovereign Ed25519 identity, written to
    :func:`attestation_path`, and -- when *chain* is supplied -- mirrored into
    the HMAC audit chain via ``record_sovereign_attestation``. The signature
    over the same canonical bytes is deterministic (Ed25519, RFC 8032), so two
    activations of the same posture at the same timestamp seal byte-identical
    bodies.

    Returns:
        The sealed :class:`PostureAttestation`.
    """
    from bernstein.core.skills.catalog.signature import sign_payload

    identity_dir = workdir.joinpath(*_ATTESTATION_SUBDIR)
    private_pem, public_pem = load_or_create_sovereign_identity(identity_dir)
    unsigned = PostureAttestation(
        profile=policy.profile,
        schema_version=EFFECTIVE_POLICY_SCHEMA_VERSION,
        posture_hash=policy.posture_hash(),
        effective_policy=policy.to_canonical_document(),
        timestamp=timestamp,
    )
    signature = sign_payload(_canonical_bytes(unsigned.signed_body()), private_pem)

    journal_entry_hash = ""
    if chain is not None:
        from bernstein.core.security.audit_chain import record_sovereign_attestation

        event = record_sovereign_attestation(
            chain=chain,
            profile=policy.profile,
            posture_hash=unsigned.posture_hash,
            signed_body=unsigned.signed_body(),
            signature=signature,
            signer_public_key_pem=public_pem,
        )
        journal_entry_hash = event.hmac

    sealed = PostureAttestation(
        profile=unsigned.profile,
        schema_version=unsigned.schema_version,
        posture_hash=unsigned.posture_hash,
        effective_policy=unsigned.effective_policy,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_pem,
        signature=signature,
        journal_entry_hash=journal_entry_hash,
    )
    path = attestation_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return sealed


def read_posture_attestation(workdir: Path) -> PostureAttestation | None:
    """Return the persisted attestation under *workdir*, or ``None``."""
    path = attestation_path(workdir)
    if not path.is_file():
        return None
    try:
        return PostureAttestation.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Drift evaluation + refusal
# ---------------------------------------------------------------------------


class PostureDriftRefusal(RuntimeError):
    """Raised at spawn time when the live posture diverges from the attestation.

    Carries the signed drift record (and its content hash) so the caller
    surfaces the proof artefact, not just an error string. Deliberately a plain
    :class:`RuntimeError` -- not a ``SpawnError`` -- so the spawner's
    per-provider failover loop never retries it: a drift refusal is a hard
    stop, exactly like the adapter security-floor refusal it mirrors.
    """

    def __init__(self, message: str, *, record: dict[str, Any], record_sha256: str) -> None:
        super().__init__(message)
        self.record = record
        self.record_sha256 = record_sha256


@dataclass(frozen=True, slots=True)
class DriftEvaluation:
    """Outcome of comparing the live posture against the attestation."""

    drifted: bool
    reason: str
    attested_hash: str
    observed_hash: str
    diverging_keys: tuple[str, ...]
    observed_policy: EffectivePolicy


def _diverging_keys(attested: Mapping[str, Any], observed: Mapping[str, Any]) -> tuple[str, ...]:
    keys = set(attested) | set(observed)
    return tuple(sorted(k for k in keys if attested.get(k) != observed.get(k)))


def evaluate_posture_drift(*, workdir: Path, config_snapshot: Mapping[str, Any] | None) -> DriftEvaluation:
    """Recompute the live posture and compare it to the stored attestation.

    An absent attestation is itself drift (the posture was never attested).
    """
    observed = resolve_effective_policy(SOVEREIGN_PROFILE, config_snapshot)
    observed_hash = observed.posture_hash()
    attestation = read_posture_attestation(workdir)
    if attestation is None:
        return DriftEvaluation(
            drifted=True,
            reason="no posture attestation on disk; the sovereign profile was never activated for this workspace",
            attested_hash="",
            observed_hash=observed_hash,
            diverging_keys=(),
            observed_policy=observed,
        )
    if attestation.posture_hash == observed_hash:
        return DriftEvaluation(
            drifted=False,
            reason="",
            attested_hash=attestation.posture_hash,
            observed_hash=observed_hash,
            diverging_keys=(),
            observed_policy=observed,
        )
    diverging = _diverging_keys(attestation.effective_policy, observed.to_canonical_document())
    return DriftEvaluation(
        drifted=True,
        reason=f"live posture {observed_hash} diverges from attested posture {attestation.posture_hash}",
        attested_hash=attestation.posture_hash,
        observed_hash=observed_hash,
        diverging_keys=diverging,
        observed_policy=observed,
    )


def record_and_sign_drift(
    *,
    workdir: Path,
    evaluation: DriftEvaluation,
    timestamp: int,
    chain: AuditChainStore | None = None,
) -> tuple[dict[str, Any], str]:
    """Sign and anchor a drift record naming the diverging keys.

    Returns ``(signed_record, record_sha256)``. The record is signed with the
    sovereign identity and, when *chain* is supplied, anchored in the audit
    chain so it re-verifies under ``bernstein audit verify``.
    """
    from bernstein.core.skills.catalog.signature import sign_payload

    identity_dir = workdir.joinpath(*_ATTESTATION_SUBDIR)
    private_pem, public_pem = load_or_create_sovereign_identity(identity_dir)
    signed_body = {
        "record_kind": _RECORD_KIND_DRIFT,
        "schema_version": EFFECTIVE_POLICY_SCHEMA_VERSION,
        "profile": SOVEREIGN_PROFILE,
        "attested_hash": evaluation.attested_hash,
        "observed_hash": evaluation.observed_hash,
        "diverging_keys": list(evaluation.diverging_keys),
        "effective_policy": evaluation.observed_policy.to_canonical_document(),
        "timestamp": timestamp,
    }
    signature = sign_payload(_canonical_bytes(signed_body), private_pem)
    record = dict(signed_body)
    record["signer_public_key_pem"] = public_pem
    record["signature"] = signature
    record_sha256 = _sha256_of(record)
    if chain is not None:
        from bernstein.core.security.audit_chain import record_sovereign_drift

        record_sovereign_drift(
            chain=chain,
            profile=SOVEREIGN_PROFILE,
            observed_hash=evaluation.observed_hash,
            signed_body=signed_body,
            signature=signature,
            signer_public_key_pem=public_pem,
        )
    return record, record_sha256


# ---------------------------------------------------------------------------
# Offline verification (wired into ``bernstein audit verify``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SovereignVerifyResult:
    """Outcome of re-verifying every sovereign attestation / drift record."""

    ok: bool
    errors: list[str] = field(default_factory=list[str])
    attestation_count: int = 0
    drift_count: int = 0


def verify_sovereign_attestations(audit_dir: Path, *, key: bytes | None = None) -> SovereignVerifyResult:
    """Re-verify sovereign attestation + drift records from the chain (offline).

    For every ``sovereign.posture_attestation`` and
    ``sovereign.posture_drift`` chain entry this recomputes the canonical
    signed body from ``event.details`` alone and re-checks the embedded Ed25519
    signature against the embedded public key, then confirms the recorded
    posture hash equals the SHA-256 of the recorded effective-policy document
    and (for drift) that the record names at least one diverging key. A mutated
    field, a forged signature, or a drift record whose hashes agree fails
    verification exactly like a tampered chain entry. Zero records is a silent
    pass (``ok=True``).
    """
    from bernstein.core.security.audit import AuditLog
    from bernstein.core.security.audit_chain import (
        EVENT_SOVEREIGN_ATTESTATION,
        EVENT_SOVEREIGN_DRIFT,
    )
    from bernstein.core.skills.catalog.signature import verify_payload

    log = AuditLog(audit_dir=audit_dir, key=key) if key is not None else AuditLog(audit_dir=audit_dir)
    att_events = log.query(event_type=EVENT_SOVEREIGN_ATTESTATION)
    drift_events = log.query(event_type=EVENT_SOVEREIGN_DRIFT)
    errors: list[str] = []

    for event in att_events:
        _verify_one_record(event.details, kind=_RECORD_KIND_ATTESTATION, errors=errors, verify_payload=verify_payload)
    for event in drift_events:
        _verify_one_record(event.details, kind=_RECORD_KIND_DRIFT, errors=errors, verify_payload=verify_payload)

    return SovereignVerifyResult(
        ok=not errors,
        errors=errors,
        attestation_count=len(att_events),
        drift_count=len(drift_events),
    )


def _verify_one_record(
    details: Mapping[str, Any],
    *,
    kind: str,
    errors: list[str],
    verify_payload: Any,
) -> None:
    body = details.get("signed_body")
    signature = details.get("signature")
    public_key = details.get("signer_public_key_pem")
    if not isinstance(body, Mapping):
        errors.append(f"{kind}: record has no signed_body")
        return
    outcome = verify_payload(
        _canonical_bytes(body),
        signature if isinstance(signature, str) else None,
        public_key if isinstance(public_key, str) else None,
        allow_unverified=True,
    )
    subject = str(body.get("posture_hash") or body.get("observed_hash") or "?")
    if not outcome.verified:
        errors.append(f"{kind} {subject}: signature check failed ({outcome.reason})")
        return
    effective_policy = body.get("effective_policy")
    if isinstance(effective_policy, Mapping):
        recomputed = _sha256_of(effective_policy)
        recorded = str(body.get("observed_hash") if kind == _RECORD_KIND_DRIFT else body.get("posture_hash"))
        if recorded != recomputed:
            errors.append(f"{kind} {subject}: recorded hash {recorded} does not match recomputed {recomputed}")
    if kind == _RECORD_KIND_DRIFT:
        diverging = body.get("diverging_keys")
        if not isinstance(diverging, list) or not diverging:
            errors.append(f"{kind} {subject}: drift record names no diverging keys")
        if body.get("attested_hash") == body.get("observed_hash"):
            errors.append(f"{kind} {subject}: drift record's attested and observed hashes agree")
