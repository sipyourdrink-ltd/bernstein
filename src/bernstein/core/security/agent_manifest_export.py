"""Composition-only Agent Manifest export with explicit operator declarations.

The optional SDK is pinned to python-v0.12.0 (9478b56). This is a snapshot of
Bernstein's composed prompt, before adapter-specific additions, not a complete
runtime identity. Exported documents are evidence and never admission inputs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    sign_detached_jws_with_signer,
    verify_detached_jws_over_canonical,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from bernstein.core.security.key_custody import KMSAdapter

SDK_VERSION = "0.12.0"
UPSTREAM_SHA = "9478b56cc349bef01441db4e17e61849c8d69d6f"
COMPANION_TYP = "bernstein-agent-manifest-availability+jws"
UNBOUND_SLOTS = (
    "policy_bundle",
    "tool_manifest",
    "model_identity",
    "rag_corpus",
    "memory_baseline",
    "decision_trace",
    "supply_chain",
    "delegation_chain",
    "hitl_record",
)


class CompositionDeclarations(BaseModel):
    """Required assertions; none is derived from a clock, config or sibling hop."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    version: Literal["0.1"]
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signed_at: AwareDatetime
    prompt_bound_at: AwareDatetime
    prompt_version: str = Field(min_length=1)
    classification: Literal["public", "internal", "confidential", "restricted"]
    key_type: Literal["software", "hsm", "tee-sealed"]
    availability_reasons: dict[str, str]

    @field_validator("prompt_version", "agent_id", "issuer")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("declarations must not be blank")
        return value

    @field_validator("availability_reasons")
    @classmethod
    def _exact_unbound_reasons(cls, values: dict[str, str]) -> dict[str, str]:
        if set(values) != set(UNBOUND_SLOTS) or any(not reason.strip() for reason in values.values()):
            raise ValueError("availability_reasons must name exactly the nine unbound slots with nonblank reasons")
        return values

    @model_validator(mode="after")
    def _time_order(self) -> CompositionDeclarations:
        if not self.prompt_bound_at <= self.signed_at or not self.issued_at <= self.signed_at < self.expires_at:
            raise ValueError("require prompt_bound_at <= signed_at and issued_at <= signed_at < expires_at")
        return self


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CompositionExport:
    """Canonical bytes; callers store references, never use these as authority."""

    unsigned_bytes: bytes
    manifest_bytes: bytes
    availability_bytes: bytes


def require_manifest_sdk() -> None:
    """Check the pinned optional dependency before any custody side effect."""
    # The task lifecycle handles ValueError after claim; ImportError would
    # escape its spawn accounting and leave the claimed task without a session.
    try:
        installed = version("agent-manifest")
    except PackageNotFoundError as exc:
        raise ValueError(f"Agent Manifest export requires agent-manifest=={SDK_VERSION}") from exc
    if installed != SDK_VERSION:
        raise ValueError(f"Agent Manifest export requires agent-manifest=={SDK_VERSION}")


def build_composition_export(
    prompt: bytes,
    declarations: CompositionDeclarations,
    signer: KMSAdapter,
) -> CompositionExport:
    """Bind only the supplied composed-prompt bytes; import the extra on use."""
    require_manifest_sdk()
    try:
        from agent_manifest import SIGNED_FIELDS, Manifest, signing_pre_image
    except ImportError as exc:
        raise ValueError(f"Agent Manifest export cannot import agent-manifest=={SDK_VERSION}") from exc

    # Revalidate mutable nested declarations at the actual signing boundary.
    declarations = CompositionDeclarations.model_validate(declarations.model_dump())
    body: dict[str, Any] = {
        "@context": "https://manifest.agentrust-io.com/v0.2/context.json",
        "@type": "AgentManifest",
        "manifest_id": declarations.manifest_id,
        "agent_id": declarations.agent_id,
        "issuer": declarations.issuer,
        "version": declarations.version,
        "issued_at": _timestamp(declarations.issued_at),
        "expires_at": _timestamp(declarations.expires_at),
        "crypto_profile": "standard",
        "profile": "composition-only",
        "unbound_artifacts": list(UNBOUND_SLOTS),
        "artifacts": {
            "system_prompt": {
                "hash": _digest(prompt),
                "hash_algorithm": "SHA-256",
                "version": declarations.prompt_version,
                "classification": declarations.classification,
                "bound_at": _timestamp(declarations.prompt_bound_at),
            }
        },
    }
    # Preserve the upstream 0.1 JSON signing preimage, including its null rules.
    Manifest.model_validate(body)
    unsigned = canonicalize_jcs(body)
    jwk = signer.public_key_jwk()
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError("Agent Manifest export requires an Ed25519 custody signer")
    raw_public = base64.urlsafe_b64decode(jwk["x"] + "==")
    if len(raw_public) != 32:
        raise ValueError("Ed25519 public key must contain 32 bytes")
    key_id = hashlib.sha256(raw_public).hexdigest()
    body["signature"] = {
        "algorithm": "Ed25519",
        "key_id": key_id,
        "key_type": declarations.key_type,
        "signed_at": _timestamp(declarations.signed_at),
        "signed_fields": list(SIGNED_FIELDS),
        "signature_value": base64.urlsafe_b64encode(signer.sign(signing_pre_image(body))).decode("ascii").rstrip("="),
    }
    Manifest.model_validate(body)
    manifest_bytes = canonicalize_jcs(body)
    companion: dict[str, Any] = {
        "schema": "bernstein.agent-manifest-availability.v1",
        "manifest_id": declarations.manifest_id,
        "manifest_digest": _digest(manifest_bytes),
        "binding_scope": "spawner-composed-prompt",
        "upstream": {"package": "agent-manifest", "version": SDK_VERSION, "source_sha": UPSTREAM_SHA},
        "availability_reasons": dict(declarations.availability_reasons),
    }
    companion["signature"] = sign_detached_jws_with_signer(
        canonicalize_jcs(companion),
        signer,
        typ=COMPANION_TYP,
        kid=key_id,
    )
    return CompositionExport(unsigned, manifest_bytes, canonicalize_jcs(companion))


def verify_availability_companion(manifest_bytes: bytes, companion_bytes: bytes, public_key_pem: bytes) -> bool:
    """Check the companion's signature and manifest binding, not runtime state.

    The caller supplies an independently trusted public key. Upstream manifest
    signature and artifact appraisal remain a separate required operation.
    """
    try:
        manifest = json.loads(manifest_bytes)
        companion = json.loads(companion_bytes)
        if not isinstance(manifest, dict) or not isinstance(companion, dict):
            return False
        signature = companion.pop("signature")
        if not isinstance(signature, str):
            return False
        if not verify_detached_jws_over_canonical(
            canonicalize_jcs(companion),
            signature,
            public_key_pem,
            expected_typ=COMPANION_TYP,
        ):
            return False
        return (
            companion["schema"] == "bernstein.agent-manifest-availability.v1"
            and companion["binding_scope"] == "spawner-composed-prompt"
            and companion["manifest_digest"] == _digest(manifest_bytes)
            and companion["manifest_id"] == manifest["manifest_id"]
            and manifest["profile"] == "composition-only"
            and set(companion["availability_reasons"]) == set(manifest["unbound_artifacts"]) == set(UNBOUND_SLOTS)
            and all(isinstance(reason, str) and reason.strip() for reason in companion["availability_reasons"].values())
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


class CompositionManifestExporter:
    """Opt-in spawner callback, with per-hop declarations and a lineage root.

    Writes a fresh directory per hop. A duplicate is an error rather than an
    overwrite. The operator owns the lineage directory and its containment.
    """

    def __init__(
        self,
        lineage_root: Path,
        declarations_for_hop: Callable[[str], CompositionDeclarations],
        signer: KMSAdapter,
    ) -> None:
        self.lineage_root = lineage_root
        self.declarations_for_hop = declarations_for_hop
        self.signer = signer

    def __call__(self, hop_id: str, prompt: str) -> list[dict[str, str]]:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", hop_id) is None:
            raise ValueError("hop_id must be a safe single path segment")
        declarations = self.declarations_for_hop(hop_id)
        export = build_composition_export(prompt.encode("utf-8"), declarations, self.signer)
        directory = self.lineage_root / "agent-manifests" / hop_id
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        references: list[dict[str, str]] = []
        for filename, content in [
            ("manifest.json", export.manifest_bytes),
            ("availability.json", export.availability_bytes),
        ]:
            path = directory / filename
            with path.open("xb") as stream:
                stream.write(content)
            path.chmod(0o600)
            references.append(
                {
                    "artifact_id": f"{declarations.manifest_id}/{filename}",
                    "resolver": path.relative_to(self.lineage_root).as_posix(),
                    "digest": _digest(content),
                }
            )
        return references
