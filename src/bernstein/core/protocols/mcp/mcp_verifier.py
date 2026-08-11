"""Signature + manifest verification for third-party MCP servers.

In the post-OpenClaw 433+ CVE era (May 2026) the MCP server registry/marketplace
is the next big supply-chain attack vector. CVE-2025-6514 alone compromised
~437K developer environments via a single unsigned ``mcp-remote`` package.

This module is the **verification entry point** that Bernstein consults
*before* loading a third-party MCP server. It performs:

1. Manifest parse + structural validation (``mcp-server.yaml``)
2. Detached **Ed25519** signature check over the JCS-canonicalized manifest
   body, using the existing ``cryptography`` primitives already vendored for
   ``agent_card_signer`` and ``commit_signing``
3. Trusted-publisher allowlist enforcement (publisher fingerprint must be
   listed; otherwise the verdict is ``UNTRUSTED_PUBLISHER`` even if the
   signature is mathematically valid)
4. Content-hash check against the manifest's declared ``content_hash``
   (when the caller provides the bundle bytes)

Sigstore (Fulcio + Rekor) verification is the second signature path the
ticket calls for. It is **deferred** to a follow-up PR (see note below) because
the substrate in :mod:`bernstein.core.security.sigstore_attestation` is
attestation-side only and the verify path needs a Rekor-fetch + bundle
parse that warrants its own review surface. Ed25519 alone is a complete,
non-mocked crypto path on its own - losing the cosign/Sigstore identity
attestation only loses the *who-published-this* signal, not the *content
integrity* signal.

Example::

    from bernstein.core.protocols.mcp.mcp_verifier import verify_mcp_server

    result = verify_mcp_server(
        manifest_yaml=Path("mcp-server.yaml").read_text(),
        signature_b64=Path("mcp-server.sig").read_text().strip(),
        publisher_public_key_pem=pem_bytes,
        trusted_publishers={"ed25519/abcd1234..."},
        bundle_bytes=Path("server.tar.gz").read_bytes(),
    )
    if not result.ok:
        raise MCPVerificationError(result.failure_reason)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ED25519_FINGERPRINT_PREFIX",
    "MANIFEST_TYP",
    "MCPSignedManifest",
    "MCPVerificationError",
    "MCPVerificationResult",
    "VerificationVerdict",
    "canonicalize_manifest",
    "parse_manifest",
    "verify_mcp_server",
]


# ---------------------------------------------------------------------------
# Constants + sentinels
# ---------------------------------------------------------------------------

#: Ed25519 fingerprint prefix used in ``publisher.fingerprint`` (e.g.
#: ``ed25519/abcd1234...``). Anchored constant so callers and tests share
#: one source of truth instead of stringly-typed prefixes.
ED25519_FINGERPRINT_PREFIX: str = "ed25519/"

#: Type tag baked into the manifest signing input - mirrors the
#: ``agent-card+jws`` typ binding used in :mod:`agent_card_signer`. Without
#: this binding, a signature minted for a different JWS context with the
#: same key would verify here.
MANIFEST_TYP: str = "mcp-server-manifest+ed25519"


class VerificationVerdict:
    """String-typed verdicts for :class:`MCPVerificationResult`.

    Kept as a class with class-vars rather than an Enum so the values are
    both comparable as plain strings (logs, JSON) and discoverable via
    ``VerificationVerdict.OK`` style references.
    """

    OK: str = "ok"
    UNSIGNED: str = "unsigned"
    BAD_SIGNATURE: str = "bad_signature"
    UNTRUSTED_PUBLISHER: str = "untrusted_publisher"
    BAD_MANIFEST: str = "bad_manifest"
    CONTENT_HASH_MISMATCH: str = "content_hash_mismatch"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPVerificationError(Exception):
    """Raised when MCP server verification fails in strict mode.

    Attributes:
        verdict: Short verdict code from :class:`VerificationVerdict`.
        manifest_name: Best-effort manifest ``name`` for log/UX context.
    """

    def __init__(self, verdict: str, message: str, *, manifest_name: str = "") -> None:
        super().__init__(message)
        self.verdict = verdict
        self.manifest_name = manifest_name


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPSignedManifest:
    """Parsed, structurally-validated ``mcp-server.yaml`` body.

    Only the fields the verifier consults are typed; the rest is preserved
    as ``raw`` so downstream consumers (scanner, marketplace) read the same
    canonical dict.

    Attributes:
        name: Server identifier (must be non-empty).
        version: Semver-ish version string (must be non-empty).
        publisher_name: Publisher display name.
        publisher_fingerprint: Ed25519 fingerprint string in
            ``ed25519/<hex>`` form, used as the trusted-publisher allowlist
            key.
        content_hash: Optional ``sha256/<hex>`` of the canonical server
            bundle. When set + a ``bundle_bytes`` is passed to the
            verifier, the bytes are hashed and compared.
        raw: Original parsed dict, for downstream inspection.
    """

    name: str
    version: str
    publisher_name: str
    publisher_fingerprint: str
    content_hash: str = ""
    raw: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class MCPVerificationResult:
    """Verifier verdict for a single MCP server manifest.

    Attributes:
        ok: True iff the manifest is signed, the signature verifies, the
            publisher is trusted, and (if present) the content hash
            matches.
        verdict: One of :class:`VerificationVerdict` codes.
        failure_reason: Human-readable explanation when ``ok=False``.
        manifest: Parsed manifest, when parsing succeeded.
        publisher_fingerprint: Echoed for log/UX surfaces.
    """

    ok: bool
    verdict: str
    failure_reason: str = ""
    manifest: MCPSignedManifest | None = None
    publisher_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible summary dict."""
        out: dict[str, Any] = {
            "ok": self.ok,
            "verdict": self.verdict,
            "failure_reason": self.failure_reason,
            "publisher_fingerprint": self.publisher_fingerprint,
        }
        if self.manifest is not None:
            out["manifest_name"] = self.manifest.name
            out["manifest_version"] = self.manifest.version
        return out


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def parse_manifest(manifest_yaml_or_json: str) -> MCPSignedManifest:
    """Parse + structurally validate an ``mcp-server.yaml`` body.

    Accepts either YAML or JSON; falls through to JSON if PyYAML is
    unavailable. Raises :class:`MCPVerificationError` with verdict
    ``BAD_MANIFEST`` on structural problems (missing required fields,
    fingerprint shape mismatch, etc.).

    Args:
        manifest_yaml_or_json: Raw manifest text.

    Returns:
        Validated :class:`MCPSignedManifest`.

    Raises:
        MCPVerificationError: When the manifest is malformed.
    """
    raw_data = _load_yaml_or_json(manifest_yaml_or_json)
    if not isinstance(raw_data, dict):
        raise MCPVerificationError(
            VerificationVerdict.BAD_MANIFEST,
            "manifest root must be a mapping",
        )

    raw: dict[str, Any] = raw_data  # type: ignore[assignment]

    name = str(raw.get("name", "")).strip()
    version = str(raw.get("version", "")).strip()
    if not name or not version:
        raise MCPVerificationError(
            VerificationVerdict.BAD_MANIFEST,
            "manifest must declare non-empty name and version",
        )

    publisher_block_raw = raw.get("publisher") or {}
    if not isinstance(publisher_block_raw, dict):
        raise MCPVerificationError(
            VerificationVerdict.BAD_MANIFEST,
            "publisher must be a mapping",
            manifest_name=name,
        )
    publisher_block: dict[str, Any] = publisher_block_raw  # type: ignore[assignment]
    publisher_name = str(publisher_block.get("name", "")).strip()
    publisher_fingerprint = str(publisher_block.get("fingerprint", "")).strip()
    if not publisher_fingerprint.startswith(ED25519_FINGERPRINT_PREFIX):
        raise MCPVerificationError(
            VerificationVerdict.BAD_MANIFEST,
            f"publisher.fingerprint must start with {ED25519_FINGERPRINT_PREFIX!r}",
            manifest_name=name,
        )

    content_hash = str(raw.get("content_hash", "")).strip()
    if content_hash and not content_hash.startswith("sha256/"):
        raise MCPVerificationError(
            VerificationVerdict.BAD_MANIFEST,
            "content_hash must start with 'sha256/'",
            manifest_name=name,
        )

    return MCPSignedManifest(
        name=name,
        version=version,
        publisher_name=publisher_name,
        publisher_fingerprint=publisher_fingerprint,
        content_hash=content_hash,
        raw=raw,
    )


def _load_yaml_or_json(text: str) -> Any:
    """Load YAML when PyYAML is present, else fall through to JSON.

    The ticket calls for a YAML manifest, but Bernstein already runs
    deployments where PyYAML is intentionally absent (audit hardening).
    JSON is a strict subset of YAML for the shapes we care about, so this
    keeps verification working in both substrate variants.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPVerificationError(
                VerificationVerdict.BAD_MANIFEST,
                f"manifest is not valid JSON (and PyYAML unavailable): {exc}",
            ) from exc
    except Exception as exc:  # pragma: no cover - PyYAML path
        raise MCPVerificationError(
            VerificationVerdict.BAD_MANIFEST,
            f"manifest YAML parse failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Canonicalization (RFC 8785 JCS - same approach as agent_card_signer)
# ---------------------------------------------------------------------------


def canonicalize_manifest(manifest: MCPSignedManifest) -> bytes:
    """Return the bytes the publisher signed and the verifier reverifies.

    Uses RFC 8785 JCS (mirrors :mod:`agent_card_signer`): sorted keys, no
    insignificant whitespace, ``ensure_ascii=False``, ``allow_nan=False``.

    The signing input is bound to :data:`MANIFEST_TYP` so a signature
    minted for a different context with the same key cannot replay against
    the manifest verifier.
    """
    body = {
        "typ": MANIFEST_TYP,
        "name": manifest.name,
        "version": manifest.version,
        "publisher": {
            "name": manifest.publisher_name,
            "fingerprint": manifest.publisher_fingerprint,
        },
        "content_hash": manifest.content_hash,
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Public verification entry point
# ---------------------------------------------------------------------------


def verify_mcp_server(
    *,
    manifest_yaml: str,
    signature_b64: str,
    publisher_public_key_pem: bytes,
    trusted_publishers: set[str],
    bundle_bytes: bytes | None = None,
) -> MCPVerificationResult:
    """Verify a third-party MCP server's manifest + signature.

    Args:
        manifest_yaml: Raw ``mcp-server.yaml`` (or JSON-equivalent) body.
        signature_b64: Base64-encoded Ed25519 signature over the canonical
            manifest body (see :func:`canonicalize_manifest`).
        publisher_public_key_pem: PEM-encoded ``SubjectPublicKeyInfo``
            Ed25519 public key supplied by the publisher / fetched from
            their identity endpoint.
        trusted_publishers: Set of fingerprints (``ed25519/<hex>``) the
            local site has explicitly trusted. A signature that verifies
            mathematically but isn't from a trusted publisher returns
            :data:`VerificationVerdict.UNTRUSTED_PUBLISHER` - never
            ``OK`` - so a stolen-key + new-fingerprint scenario doesn't
            silently pass.
        bundle_bytes: Optional canonical server bundle. When provided
            **and** the manifest declares a ``content_hash``, the bytes
            are SHA-256'd and compared.

    Returns:
        :class:`MCPVerificationResult`. Callers map this to strict-deny or
        warn-only behavior via :mod:`mcp_signing_policy`.
    """
    try:
        manifest = parse_manifest(manifest_yaml)
    except MCPVerificationError as exc:
        return MCPVerificationResult(
            ok=False,
            verdict=exc.verdict,
            failure_reason=str(exc),
        )

    if not isinstance(signature_b64, str):
        # Declared ``str``, but this is the boundary where a third party's
        # signature arrives: a JSON ``null`` or a raw ``Path.read_bytes()``
        # reaches here from callers this module does not type-check. The
        # contract is to return a verdict, so a non-string is refused rather
        # than surfacing as an ``AttributeError`` from ``.strip()`` below.
        #
        # ``None`` is a signature that was never supplied; anything else is a
        # supplied signature that cannot be read. Policy maps UNSIGNED and
        # BAD_SIGNATURE to different outcomes, so the two stay distinct.
        return MCPVerificationResult(
            ok=False,
            verdict=(VerificationVerdict.UNSIGNED if signature_b64 is None else VerificationVerdict.BAD_SIGNATURE),
            failure_reason=(
                "no signature provided"
                if signature_b64 is None
                else f"signature must be a base64 string, got {type(signature_b64).__name__}"
            ),
            manifest=manifest,
            publisher_fingerprint=manifest.publisher_fingerprint,
        )

    if not signature_b64.strip():
        return MCPVerificationResult(
            ok=False,
            verdict=VerificationVerdict.UNSIGNED,
            failure_reason="no signature provided",
            manifest=manifest,
            publisher_fingerprint=manifest.publisher_fingerprint,
        )

    if manifest.publisher_fingerprint not in trusted_publishers:
        return MCPVerificationResult(
            ok=False,
            verdict=VerificationVerdict.UNTRUSTED_PUBLISHER,
            failure_reason=(
                f"publisher fingerprint {manifest.publisher_fingerprint!r} is not in the trusted-publisher allowlist"
            ),
            manifest=manifest,
            publisher_fingerprint=manifest.publisher_fingerprint,
        )

    if not _verify_ed25519(
        signing_input=canonicalize_manifest(manifest),
        signature_b64=signature_b64,
        public_key_pem=publisher_public_key_pem,
    ):
        return MCPVerificationResult(
            ok=False,
            verdict=VerificationVerdict.BAD_SIGNATURE,
            failure_reason="Ed25519 signature did not verify against manifest",
            manifest=manifest,
            publisher_fingerprint=manifest.publisher_fingerprint,
        )

    if manifest.content_hash and bundle_bytes is not None:
        expected = manifest.content_hash.removeprefix("sha256/").lower()
        actual = hashlib.sha256(bundle_bytes).hexdigest()
        if expected != actual:
            return MCPVerificationResult(
                ok=False,
                verdict=VerificationVerdict.CONTENT_HASH_MISMATCH,
                failure_reason=(
                    f"content_hash mismatch: manifest declared sha256/{expected!r}, computed sha256/{actual!r}"
                ),
                manifest=manifest,
                publisher_fingerprint=manifest.publisher_fingerprint,
            )

    return MCPVerificationResult(
        ok=True,
        verdict=VerificationVerdict.OK,
        manifest=manifest,
        publisher_fingerprint=manifest.publisher_fingerprint,
    )


# ---------------------------------------------------------------------------
# Internal Ed25519 verify (cryptography)
# ---------------------------------------------------------------------------


def _verify_ed25519(
    *,
    signing_input: bytes,
    signature_b64: str,
    public_key_pem: bytes,
) -> bool:
    """Return True iff Ed25519 signature verifies under public_key_pem.

    Reuses the same ``cryptography`` package already vendored for
    :mod:`agent_card_signer` - no new heavy deps.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        signature = base64.b64decode(signature_b64.encode("ascii"), validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False

    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError):
        return False

    if not isinstance(public_key, Ed25519PublicKey):
        # public_key was parsed but is not Ed25519 (e.g. RSA PEM passed in).
        # Previously this surfaced as the TypeError/AttributeError raised by
        # the differing verify() signatures of the other key types.
        return False

    try:
        public_key.verify(signature, signing_input)
    except InvalidSignature:
        return False
    except (TypeError, AttributeError):
        # Retained so malformed (non-bytes) arguments from untyped callers
        # keep returning False rather than propagating.
        return False
    return True


# ---------------------------------------------------------------------------
# Sigstore + Rekor verification path
# ---------------------------------------------------------------------------
# The ticket calls for both an Ed25519 content-integrity signature *and* a
# Sigstore identity-attestation signature (Fulcio short-lived cert + Rekor
# transparency log entry). The Bernstein substrate has the *attestation*
# direction in :mod:`bernstein.core.security.sigstore_attestation`, but the
# verify direction needs:
#
#   1. Sigstore bundle parse (DSSE envelope or in-toto attestation)
#   2. Fulcio cert chain verify against the trust root
#   3. Rekor inclusion proof + SET signature verify
#   4. Identity claim (publisher SAN) match against the manifest
#
# That surface justifies its own PR + threat-model review (especially the
# trust-root pinning vs. TUF rotation question). Tracked under the same
# ticket. Until then, ``verify_mcp_server`` is Ed25519-only.
