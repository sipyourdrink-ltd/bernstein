"""HTTP Message Signatures (RFC 9421) over outbound agent-facing requests.

Bernstein already anchors a per-install Ed25519 keypair in the persistent
:class:`~bernstein.core.security.agent_card_keystore.AgentCardKeystore`
(``.bernstein/keys/``), published as a JWKS at
``/.well-known/agent.json/keys``. That keypair *is* the signed install
identity for outbound traffic: this module signs each outbound HTTP request
with it so a receiving site can prove the request came from a specific,
verifiable Bernstein install rather than anonymous automation.

Wire format
-----------
We emit the two RFC 9421 structured-field headers over a fixed set of
covered components::

    Signature-Input: sig1=("@method" "@target-uri");created=<ts>;keyid="<kid>";alg="ed25519"
    Signature: sig1=:<base64 ed25519 signature>:

* ``@method`` and ``@target-uri`` are the RFC 9421 derived components; when
  a ``Content-Digest`` header is present it is added to the covered set so
  the body is bound to the signature too.
* ``keyid`` is the RFC 7638 JWK thumbprint of the install-identity public
  key (:func:`install_identity_keyid`). It is what ties every signature to
  the install identity: the thumbprint changes when the identity rotates,
  so a signature made under the old key names a ``keyid`` that is no longer
  present in the current key directory - verification then fails
  deterministically (see :func:`build_key_directory` / :func:`verify_request`).
* ``alg`` is ``ed25519`` (RFC 9421 §3.3.6, EdDSA over Curve25519).

Signing-required mode
---------------------
When ``BERNSTEIN_HTTP_SIGNING_REQUIRED=1`` (or a caller passes
``require_signature=True``) an outbound path that produced no signature is a
hard error: :class:`UnsignedRequestRefused`. This is the correctness gate in
issue #2305 AC5 - a signing-required deployment must never emit unsigned
agent-facing traffic.

Attestation
-----------
:func:`record_signature` appends a signing event to the HMAC-chained audit
log so every outbound signature is independently reconstructable offline.
The signature header value is chained into the tamper-evident audit chain;
stripping the install identity collapses that attestation, not merely a log
line.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bernstein.core.security.agent_card_signer import ed25519_public_jwk

if TYPE_CHECKING:
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

logger = logging.getLogger(__name__)

__all__ = [
    "ENV_KEY_DIR",
    "ENV_SIGNING_REQUIRED",
    "SIGNATURE_LABEL",
    "UnsignedRequestRefused",
    "build_key_directory",
    "default_keystore",
    "install_identity_keyid",
    "record_signature",
    "sign_outbound",
    "sign_request",
    "signing_required",
    "verify_request",
]

#: Structured-field label for the single signature this module emits.
SIGNATURE_LABEL: Final[str] = "sig1"

#: RFC 9421 algorithm token for EdDSA over Curve25519.
_ALG: Final[str] = "ed25519"

#: Environment flag that turns unsigned outbound paths into hard errors.
ENV_SIGNING_REQUIRED: Final[str] = "BERNSTEIN_HTTP_SIGNING_REQUIRED"

#: Environment override for the install-identity key directory. Shares the
#: name used by the ``/.well-known/agent.json/keys`` route so the signing key
#: and the published key directory are always the same keypair.
ENV_KEY_DIR: Final[str] = "BERNSTEIN_AGENT_CARD_KEY_DIR"


class UnsignedRequestRefused(RuntimeError):
    """Raised when signing is required but a request carried no signature."""


def signing_required() -> bool:
    """Return whether outbound signing is mandatory for this process.

    True when ``BERNSTEIN_HTTP_SIGNING_REQUIRED`` is set to ``"1"``.
    """
    return os.environ.get(ENV_SIGNING_REQUIRED, "").strip() == "1"


# ---------------------------------------------------------------------------
# Install-identity key thumbprint (RFC 7638)
# ---------------------------------------------------------------------------


def install_identity_keyid(public_pem: bytes) -> str:
    """Return the RFC 7638 JWK thumbprint of the install-identity public key.

    The thumbprint is ``base64url(sha256(canonical-jwk))`` over the required
    OKP members ``crv``, ``kty``, ``x`` (lexicographically ordered, no
    whitespace) per RFC 7638 §3. It is stable for a given public key and
    changes the instant the install identity rotates - which is exactly the
    property AC2 needs: an old signature's ``keyid`` no longer resolves in
    the rotated key directory.

    Args:
        public_pem: SPKI PEM bytes of the install-identity Ed25519 public
            key, as produced by the keystore.

    Returns:
        A short, URL-safe thumbprint string suitable as an HTTP-sig ``keyid``.
    """
    raw = serialization.load_pem_public_key(public_pem).public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": x},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256(canonical).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Key directory (published JWKS for verification)
# ---------------------------------------------------------------------------


def build_key_directory(
    keystore: AgentCardKeystore,
    *,
    include_archived: bool = True,
) -> dict[str, Any]:
    """Return the published key directory (JWKS) for verifying signatures.

    The directory always contains the current install-identity public key,
    keyed by its thumbprint. When ``include_archived`` is True any archived
    keys still inside the keystore's grace window are appended so verifiers
    that fetched an earlier directory keep validating in-flight requests.

    For rotation-invalidation semantics (AC2), callers pass
    ``include_archived=False`` to model "the operator rotated and the old key
    is gone": an old signature then fails because its ``keyid`` is absent.

    Args:
        keystore: The install-identity keystore.
        include_archived: Whether to append in-grace-window archived keys.

    Returns:
        ``{"keys": [<jwk>, ...]}`` where each JWK's ``kid`` is the
        install-identity thumbprint of that key.
    """
    _private_pem, public_pem = keystore.load_or_generate()
    kid = install_identity_keyid(public_pem)
    keys: list[dict[str, str]] = [ed25519_public_jwk(public_pem, kid=kid)]
    if include_archived:
        for archived in keystore.list_archived():
            akid = install_identity_keyid(archived.public_pem)
            keys.append(ed25519_public_jwk(archived.public_pem, kid=akid))
    return {"keys": keys}


def _public_key_from_jwk(jwk: dict[str, str]) -> Ed25519PublicKey:
    """Reconstruct an Ed25519 public key from an OKP JWK."""
    x = jwk["x"]
    pad = -len(x) % 4
    raw = base64.urlsafe_b64decode(x + ("=" * pad))
    return Ed25519PublicKey.from_public_bytes(raw)


# ---------------------------------------------------------------------------
# Signing input (RFC 9421 §2.5 signature base)
# ---------------------------------------------------------------------------


def _covered_components(headers: dict[str, str]) -> list[str]:
    """Return the ordered list of covered component identifiers.

    Always covers the ``@method`` and ``@target-uri`` derived components.
    When a ``Content-Digest`` header is present the body is bound too.
    """
    covered = ["@method", "@target-uri"]
    if _header_lookup(headers, "content-digest") is not None:
        covered.append("content-digest")
    return covered


def _header_lookup(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _signature_base(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    covered: list[str],
    created: int,
    keyid: str,
) -> tuple[bytes, str]:
    """Build the RFC 9421 signature base bytes and the ``Signature-Input`` value.

    Returns ``(base_bytes, signature_input_value)`` where the input value is
    the structured-field parameters string (without the ``sig1=`` label).
    """
    lines: list[str] = []
    for comp in covered:
        if comp == "@method":
            lines.append(f'"@method": {method.upper()}')
        elif comp == "@target-uri":
            lines.append(f'"@target-uri": {url}')
        else:
            value = _header_lookup(headers, comp) or ""
            lines.append(f'"{comp}": {value.strip()}')
    inner = " ".join(f'"{c}"' for c in covered)
    params = f'({inner});created={created};keyid="{keyid}";alg="{_ALG}"'
    lines.append(f'"@signature-params": {params}')
    base = "\n".join(lines).encode("utf-8")
    return base, params


# ---------------------------------------------------------------------------
# Sign
# ---------------------------------------------------------------------------


def sign_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    keystore: AgentCardKeystore,
    created: int | None = None,
) -> dict[str, str]:
    """Return ``headers`` augmented with RFC 9421 signature headers.

    The request is signed with the install-identity Ed25519 private key. The
    returned dict is a *copy* of ``headers`` plus ``Signature-Input`` and
    ``Signature``; the caller merges it into the outbound request.

    Args:
        method: HTTP method (``GET``/``POST``/...).
        url: Absolute request target URI.
        headers: Existing request headers (a ``Content-Digest`` here is
            folded into the covered set).
        keystore: The install-identity keystore providing the signing key.
        created: Optional signature-creation unix timestamp; defaults to the
            current time. Exposed for deterministic tests.

    Returns:
        A new headers dict including the two signature headers.
    """
    import time

    private_pem, public_pem = keystore.load_or_generate()
    keyid = install_identity_keyid(public_pem)
    ts = int(created if created is not None else time.time())

    covered = _covered_components(headers)
    base, params = _signature_base(method=method, url=url, headers=headers, covered=covered, created=ts, keyid=keyid)

    private_key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):  # pragma: no cover - keystore invariant
        msg = "install identity key is not Ed25519"
        raise TypeError(msg)
    signature = private_key.sign(base)
    sig_b64 = base64.b64encode(signature).decode("ascii")

    out = dict(headers)
    out["Signature-Input"] = f"{SIGNATURE_LABEL}={params}"
    out["Signature"] = f"{SIGNATURE_LABEL}=:{sig_b64}:"
    return out


def default_keystore() -> AgentCardKeystore:
    """Return the process install-identity keystore for outbound signing.

    Resolves the key directory from ``BERNSTEIN_AGENT_CARD_KEY_DIR`` (same
    override the ``/.well-known/agent.json/keys`` route honours) so the
    outbound signing key and the published key directory are one keypair.
    """
    from pathlib import Path

    from bernstein.core.security.agent_card_keystore import (
        DEFAULT_KEY_DIR,
        AgentCardKeystore,
    )

    override = os.environ.get(ENV_KEY_DIR, "").strip()
    key_dir = Path(override) if override else DEFAULT_KEY_DIR
    return AgentCardKeystore(key_dir)


def sign_outbound(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    call_site: str,
    keystore: AgentCardKeystore | None = None,
    audit_dir: Any = None,
) -> dict[str, str]:
    """Sign an outbound agent-facing request and attest it, all best-effort.

    This is the single entry point outbound call sites (A2A, browser/research
    activities) use. It signs with the install identity, records the signature
    into the HMAC-chained audit log when ``audit_dir`` is given, and returns
    the augmented headers to merge into the request.

    Signing never blocks egress on its own: a keystore/permission failure is
    logged and the original headers are returned unchanged - *unless*
    :func:`signing_required` is set, in which case a signing failure is a hard
    :class:`UnsignedRequestRefused` (issue #2305 AC5).

    Args:
        method: HTTP method.
        url: Absolute target URI.
        headers: Existing request headers (copied, not mutated).
        call_site: Symbolic outbound-path name for the audit record.
        keystore: Optional keystore; defaults to :func:`default_keystore`.
        audit_dir: Optional audit directory for signature attestation.

    Returns:
        Headers including the RFC 9421 signature pair (or the originals when
        signing was skipped and not required).
    """
    from bernstein.core.security.sanitize import sanitize_log

    base_headers = dict(headers or {})
    ks = keystore or default_keystore()
    try:
        _private_pem, public_pem = ks.load_or_generate()
        keyid = install_identity_keyid(public_pem)
        signed = sign_request(method=method, url=url, headers=base_headers, keystore=ks)
    except Exception as exc:  # keystore/permission failure
        if signing_required():
            raise UnsignedRequestRefused(
                f"cannot sign outbound request to {sanitize_log(url)} but "
                f"{ENV_SIGNING_REQUIRED}=1 requires a signature: {exc}"
            ) from exc
        logger.warning(
            "outbound request signing skipped for %s: %s",
            sanitize_log(url),
            sanitize_log(str(exc)),
        )
        return base_headers

    if audit_dir is not None:
        record_signature(
            audit_dir=audit_dir,
            method=method,
            url=url,
            signature_headers=signed,
            keyid=keyid,
            call_site=call_site,
        )
    return signed


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _parse_signature_input(value: str) -> tuple[list[str], dict[str, str]] | None:
    """Parse a ``Signature-Input`` value into (covered, params).

    Returns ``None`` when the value is not the ``sig1=(...)...`` shape this
    module emits. Only the single ``sig1`` label is understood.
    """
    prefix = f"{SIGNATURE_LABEL}="
    if not value.startswith(prefix):
        return None
    body = value[len(prefix) :]
    if not body.startswith("("):
        return None
    close = body.find(")")
    if close == -1:
        return None
    inner = body[1:close].strip()
    covered = [tok.strip().strip('"') for tok in inner.split(" ") if tok.strip()]
    params: dict[str, str] = {}
    for chunk in body[close + 1 :].split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        pkey, _, pval = chunk.partition("=")
        params[pkey.strip()] = pval.strip().strip('"')
    return covered, params


def _parse_signature(value: str) -> bytes | None:
    """Parse a ``Signature`` value ``sig1=:<b64>:`` into raw signature bytes."""
    prefix = f"{SIGNATURE_LABEL}=:"
    if not value.startswith(prefix) or not value.endswith(":"):
        return None
    b64 = value[len(prefix) : -1]
    try:
        return base64.b64decode(b64)
    except (ValueError, TypeError):
        return None


def verify_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    key_directory: dict[str, Any],
    require_signature: bool = False,
) -> bool:
    """Verify the RFC 9421 signature on a request against a key directory.

    Args:
        method: HTTP method that was signed.
        url: Absolute target URI that was signed.
        headers: Request headers including ``Signature-Input``/``Signature``.
        key_directory: JWKS as produced by :func:`build_key_directory`.
        require_signature: When True, a missing signature raises
            :class:`UnsignedRequestRefused` instead of returning ``False``
            (issue #2305 AC5, the signing-required gate).

    Returns:
        ``True`` iff the signature is present, its ``keyid`` resolves to a
        published key, and the Ed25519 signature verifies over the
        recomputed signature base. ``False`` otherwise (unless
        ``require_signature`` promotes a missing signature to a raise).
    """
    sig_input = _header_lookup(headers, "signature-input")
    sig_value = _header_lookup(headers, "signature")
    if sig_input is None or sig_value is None:
        if require_signature:
            raise UnsignedRequestRefused(
                f"outbound request carried no HTTP Message Signature but {ENV_SIGNING_REQUIRED}=1 requires one"
            )
        return False

    parsed = _parse_signature_input(sig_input)
    signature = _parse_signature(sig_value)
    if parsed is None or signature is None:
        return False
    covered, params = parsed
    keyid = params.get("keyid", "")
    created_raw = params.get("created", "")
    try:
        created = int(created_raw)
    except ValueError:
        return False

    jwk = None
    for candidate in key_directory.get("keys", []):
        if candidate.get("kid") == keyid:
            jwk = candidate
            break
    if jwk is None:
        # keyid absent from the current directory: e.g. the install identity
        # rotated and this signature was made under the retired key.
        return False

    base, _params = _signature_base(
        method=method, url=url, headers=headers, covered=covered, created=created, keyid=keyid
    )
    public_key = _public_key_from_jwk(jwk)
    try:
        public_key.verify(signature, base)
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------------------
# Audit attestation
# ---------------------------------------------------------------------------


def record_signature(
    *,
    audit_dir: Any,
    method: str,
    url: str,
    signature_headers: dict[str, str],
    keyid: str,
    call_site: str,
) -> None:
    """Append an outbound-signature event to the HMAC-chained audit log.

    Chains the emitted signature header value into the tamper-evident audit
    chain so an auditor can reconstruct, offline, which outbound requests
    this install signed and with which install-identity key. Best-effort:
    an audit failure never blocks the outbound request itself.

    Args:
        audit_dir: Directory backing the :class:`AuditLog` for this run.
        method: HTTP method of the signed request.
        url: Target URI of the signed request.
        signature_headers: The ``Signature-Input``/``Signature`` pair.
        keyid: Install-identity thumbprint used as the signature ``keyid``.
        call_site: Symbolic name of the outbound path (e.g. ``a2a.card``).
    """
    from bernstein.core.security.sanitize import sanitize_log

    try:
        from bernstein.core.security.audit import AuditLog

        log = AuditLog(audit_dir=audit_dir)
        log.log(
            event_type="identity.http_signature",
            actor="orchestrator",
            resource_type="outbound_request",
            resource_id=sanitize_log(url),
            details={
                "method": method.upper(),
                "call_site": call_site,
                "keyid": keyid,
                "signature_input": signature_headers.get("Signature-Input", ""),
                "signature": signature_headers.get("Signature", ""),
            },
        )
    except Exception as exc:  # attestation must never break egress
        logger.warning(
            "failed to record outbound signature attestation for %s: %s",
            sanitize_log(url),
            sanitize_log(str(exc)),
        )
