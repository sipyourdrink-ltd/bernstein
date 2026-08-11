"""Pluggable KMS adapters for lineage v2 regulatory signatures.

This module narrows the integration shape between
:class:`bernstein.core.persistence.lineage_signer.LineageSigner` and
the customer's key-management material. The Phase-1 signer wired the
private key directly off disk, which is fine for fixtures and small
single-host deployments but not for the typical sovereign-customer
shape where the key lives in:

* a Kubernetes ``Secret`` mounted as an env var,
* a PKCS#11 / Cloud-HSM device behind ``signtool``-style RPC,
* a vault-injected file rotated by an external rotator.

The :class:`KMSAdapter` protocol formalises the call surface every
backend must implement so the orchestrator does not care where the
key bytes live -- it only cares that ``sign(payload)`` returns a
detached Ed25519 signature and ``public_key_jwk()`` returns a JWK
suitable for handing to the auditor.

Design notes
------------
* Sync API only. Lineage emit is on the orchestrator critical path
  (every WAL append). An async signer would force the writer onto
  asyncio plumbing for a sub-millisecond op. HSM round-trips are the
  one case where async would matter, and the canonical PKCS#11
  pattern is to front the HSM with a low-latency local daemon
  (pkcs11-proxy / fortanix-em-cli) -- still sync from the caller's
  perspective.
* No batching. Every lineage record signs over its own canonical
  bytes; batching would require a larger refactor of the writer and
  buys nothing on the read path.
* Rotation-by-construction. ``KMSAdapter`` instances are immutable;
  the orchestrator instantiates a new adapter per run. Callers
  rotate keys by reconfiguring ``lineage.kms_adapter`` and starting
  a new run -- the verifier walks per-record JWKs (future phase) or
  trusts the operator-supplied public-key bundle (current phase).
* Public-key advertisement via JWK because every modern auditor
  tooling (cosign, sigstore-python, ssh-keygen with ``-m JWK``)
  speaks RFC 7517 JWK. Raw bytes get rejected by half the tooling
  fleet on first contact.

The module ships three concrete implementations:

* :class:`FileBasedKMSAdapter` -- reads PEM/raw Ed25519 from disk.
  Used in tests and small deployments where the key sits beside the
  config file.
* :class:`EnvBasedKMSAdapter` -- reads PEM-encoded Ed25519 from an
  environment variable. Useful for K8s ``Secret`` mounts and any
  deployment where the operator ships the key as a literal env var.
* :class:`HSMKMSAdapter` -- stub that documents the integration
  shape (PKCS#11 / Cloud-HSM) and raises :class:`NotImplementedError`
  on every method. Customers who need HSM integration subclass and
  override two methods; the rest of the lineage stack stays unchanged.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from cryptography.hazmat.primitives import serialization

from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    LineageSignerError,
    _load_ed25519_private,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class KMSAdapter(Protocol):
    """KMS-shaped signer that lineage v2 records sign through.

    Every adapter is expected to be:

    * **Sync** -- ``sign`` is on the lineage critical path.
    * **Stateless after construction** -- one adapter per run.
    * **Self-validating** -- a missing/unreadable key fails fast at
      construction so the orchestrator never emits an unsigned record
      under a "signed" config.

    Implementations also satisfy
    :class:`bernstein.core.persistence.lineage_signer.LineageSigner`
    because the lineage writer dispatches through that narrower
    surface; ``KMSAdapter`` simply layers public-key advertisement on
    top so the verifier path doesn't have to load the key twice.
    """

    def sign(self, payload: bytes) -> bytes:
        """Return a detached Ed25519 signature over *payload*.

        Args:
            payload: Canonicalised record bytes (see
                :func:`bernstein.core.persistence.lineage.canonical_record_bytes`).

        Returns:
            Raw 64-byte Ed25519 signature.

        Raises:
            LineageSignerError: When the underlying key material is
                not available or rejects the operation.
        """
        ...

    def public_key_jwk(self) -> dict[str, str]:
        """Return the verifying key as an RFC 7517 JWK.

        The JWK has ``kty='OKP'``, ``crv='Ed25519'``, ``x=<base64url>``
        and an optional ``kid``. Compliance teams use this to pin the
        key the auditor must trust without distributing raw bytes.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _public_key_jwk(public_key: Ed25519PublicKey, *, kid: str | None = None) -> dict[str, str]:
    """Return the RFC 7517 JWK encoding for *public_key*.

    The encoding follows RFC 8037 (CFRG curves in JOSE) -- ``kty='OKP'``,
    ``crv='Ed25519'``, ``x`` is base64url-no-pad of the 32-byte raw
    public key. We always include ``alg='EdDSA'`` so verifying tools
    that pick the algorithm from the JWK don't have to second-guess.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    jwk: dict[str, str] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "x": x,
    }
    if kid:
        jwk["kid"] = kid
    return jwk


# ---------------------------------------------------------------------------
# File-backed adapter
# ---------------------------------------------------------------------------


class FileBasedKMSAdapter:
    """KMS adapter that reads an Ed25519 private key from disk.

    Identical key-loading semantics to
    :class:`Ed25519FileKeySigner` (PEM PKCS#8 or raw 32-byte seed) so
    existing fixtures keep working unchanged. The disk read happens at
    construction so a missing/corrupt key fails fast.

    Use this in:

    * tests / fixtures,
    * small single-host deployments where the key file lives next to
      the config,
    * dev loops where the operator does not want to plumb env vars
      through systemd / k8s.
    """

    __slots__ = ("_signer", "key_path", "kid")

    def __init__(self, key_path: Path, *, kid: str | None = None) -> None:
        self.key_path = key_path
        self.kid = kid
        self._signer = Ed25519FileKeySigner.from_path(key_path)

    def sign(self, payload: bytes) -> bytes:
        return self._signer.sign(payload)

    def public_key_jwk(self) -> dict[str, str]:
        # Reach into the underlying signer's private key to derive the
        # public key once. Keeping the JWK derivation here -- not on
        # the signer -- means the lineage_signer module stays at a
        # narrower contract (sign/verify only).
        public_key = self._signer._private_key.public_key()
        return _public_key_jwk(public_key, kid=self.kid or self.key_path.name)


# ---------------------------------------------------------------------------
# Env-backed adapter
# ---------------------------------------------------------------------------


class EnvBasedKMSAdapter:
    """KMS adapter that reads a PEM-encoded Ed25519 key from an env var.

    Designed for K8s deployments where the customer's key lives in a
    ``Secret`` mounted as ``LINEAGE_SIGNING_KEY=...``. The env var
    payload must be PEM PKCS#8 (the format ``openssl genpkey -algorithm
    Ed25519`` emits) -- raw 32-byte keys do not survive shell
    quoting cleanly and would force base64 decoding here, which is
    one more failure surface.

    The constructor copies the env value into a private buffer and
    immediately unsets the env var (best-effort) so a downstream
    subprocess does not inherit the secret. The private key bytes
    never touch disk.
    """

    __slots__ = ("_signer", "env_var", "kid")

    def __init__(self, env_var: str, *, kid: str | None = None, scrub_env: bool = True) -> None:
        self.env_var = env_var
        self.kid = kid
        raw = os.environ.get(env_var)
        if raw is None or not raw.strip():
            raise LineageSignerError(
                f"env var {env_var!r} is not set or empty (EnvBasedKMSAdapter requires PEM Ed25519)",
            )
        # The env var carries either a PEM block (with literal newlines or
        # \\n escaped) or a raw 32-byte hex/base64 dump. We support the
        # PEM case + raw32 with prefix ``raw:`` for completeness.
        data = _decode_env_key(raw)
        # Reuse the on-disk loader's parser so the error messages are
        # identical regardless of source -- one less surprise for the
        # operator who flips between file/env adapters.
        private_key = _load_ed25519_private(data, Path(f"<env:{env_var}>"))
        if scrub_env:
            # Best-effort scrub. We cannot guarantee no other thread/
            # process snapshotted ``os.environ`` between import and now,
            # but this closes the most common leak path (subprocess.Popen
            # inheriting the env).
            with _suppressed():
                del os.environ[env_var]
        self._signer = _PrivateKeySigner(private_key)

    def sign(self, payload: bytes) -> bytes:
        return self._signer.sign(payload)

    def public_key_jwk(self) -> dict[str, str]:
        return _public_key_jwk(self._signer.public_key(), kid=self.kid or self.env_var)


def _decode_env_key(raw: str) -> bytes:
    """Decode the env-var payload into raw bytes for the PEM/raw loader.

    Accepts:

    * ``-----BEGIN PRIVATE KEY-----...`` PEM (with literal newlines).
    * Same with ``\\n`` escape sequences (k8s ConfigMap-style flatten).
    * ``raw:<hex>`` -- raw 32-byte private key in lowercase hex.
    * ``rawb64:<base64>`` -- raw 32-byte private key in base64.
    """
    stripped = raw.strip()
    if stripped.startswith("raw:"):
        try:
            return bytes.fromhex(stripped[4:])
        except ValueError as exc:
            raise LineageSignerError("EnvBasedKMSAdapter: 'raw:' payload is not valid hex") from exc
    if stripped.startswith("rawb64:"):
        try:
            return base64.b64decode(stripped[7:], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise LineageSignerError("EnvBasedKMSAdapter: 'rawb64:' payload is not valid base64") from exc
    # PEM path -- normalise escaped newlines so the cryptography parser
    # accepts it.
    if "\\n" in stripped and "\n" not in stripped:
        stripped = stripped.replace("\\n", "\n")
    return stripped.encode("utf-8")


class _PrivateKeySigner:
    """Tiny in-memory signer that holds an already-loaded private key.

    Mirrors the ``sign``/``public_key`` surface of
    :class:`Ed25519FileKeySigner` without the disk path bookkeeping so
    the env adapter can hold the parsed key without leaking the raw
    bytes back into a temp file.
    """

    __slots__ = ("_private_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    def sign(self, payload: bytes) -> bytes:
        return self._private_key.sign(payload)

    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()


class _suppressed:
    """Tiny context manager that swallows any exception (used for env scrub)."""

    def __enter__(self) -> _suppressed:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return True


# ---------------------------------------------------------------------------
# HSM stub
# ---------------------------------------------------------------------------


class HSMKMSAdapter:
    """Documentation stub for an HSM / Cloud-KMS-backed adapter.

    Bernstein does NOT ship a working PKCS#11 / Cloud-HSM client because
    the integration shape is wildly customer-specific (token slot
    layout, PIN delivery, FIPS mode, vendor-specific URI schemes).
    Instead, this class documents the required interface so a customer
    can drop in their integration in a single subclass.

    A real implementation:

    1. Resolves a PKCS#11 token URI (e.g.
       ``pkcs11:object=lineage-key;type=private``) via ``python-pkcs11``
       or the vendor SDK.
    2. Performs a session ``C_Login`` with a PIN sourced from the
       operator's secret store (Vault, K8s ``Secret``, etc.).
    3. Implements :meth:`sign` by routing the ``C_Sign`` operation to
       the loaded session over an Ed25519 key handle.
    4. Caches the public key bytes (read once at construction) and
       formats them as a JWK for :meth:`public_key_jwk`.

    Recommended PKCS#11 driver: ``python-pkcs11`` (works with SoftHSM2
    for tests, YubiHSM for production). Recommended cloud driver:
    ``cryptography`` + the cloud's KMS SDK (AWS KMS, GCP Cloud KMS,
    Azure Key Vault) talking through a thin wrapper that wraps the
    vendor's ``Sign`` / ``GetPublicKey`` calls.

    Args:
        token_uri: PKCS#11 token URI or cloud KMS resource path.
        kid: Optional JWK key id; defaults to ``token_uri`` digest.

    Raises:
        NotImplementedError: Always. Subclass and override
            :meth:`sign` and :meth:`public_key_jwk` with vendor-specific
            logic.
    """

    __slots__ = ("kid", "token_uri")

    def __init__(self, token_uri: str, *, kid: str | None = None) -> None:
        self.token_uri = token_uri
        self.kid = kid

    def sign(self, payload: bytes) -> bytes:
        del payload  # documentation stub: never actually used
        raise NotImplementedError(
            "HSMKMSAdapter is a documentation stub. Subclass and override sign() "
            "with a PKCS#11 / Cloud-KMS C_Sign call. See module docstring for "
            "recommended drivers (python-pkcs11, AWS KMS, GCP Cloud KMS, "
            "Azure Key Vault).",
        )

    def public_key_jwk(self) -> dict[str, str]:
        raise NotImplementedError(
            f"HSMKMSAdapter.public_key_jwk() must be overridden to return the "
            f"verifying key for token_uri={self.token_uri!r} as an RFC 7517 "
            f"OKP/Ed25519 JWK. See module docstring.",
        )


# ---------------------------------------------------------------------------
# Config dispatch
# ---------------------------------------------------------------------------


def kms_adapter_from_config(
    *,
    enabled: bool,
    kind: str | None = None,
    key_path: str | None = None,
    env_var: str | None = None,
    token_uri: str | None = None,
    kid: str | None = None,
) -> KMSAdapter | None:
    """Build a :class:`KMSAdapter` from ``bernstein.yaml``-shaped config.

    Recognised values for ``kind`` (case-insensitive):

    * ``"file"`` -- :class:`FileBasedKMSAdapter` (requires ``key_path``).
    * ``"env"``  -- :class:`EnvBasedKMSAdapter` (requires ``env_var``).
    * ``"hsm"``  -- HSM-backed adapter (requires ``token_uri``). The
      dispatcher prefers a concrete :class:`HSMKMSAdapter` subclass on
      the classpath that overrides both ``sign`` and ``public_key_jwk``.
      Falling back to the bare stub is gated behind
      ``BERNSTEIN_ALLOW_HSM_STUB=1`` -- a misconfigured ``hsm`` adapter
      without either a subclass or the opt-in flag raises
      :class:`LineageSignerError` at config-load time rather than
      crashing on the first ``sign()`` call inside the orchestrator.

    Returns ``None`` when ``enabled=False`` or no kind is configured,
    so callers can disable signing without removing the config block.
    """
    if not enabled:
        return None
    resolved = (kind or "file").lower().strip()
    if resolved == "file":
        if not key_path:
            raise LineageSignerError("lineage.kms_adapter=file requires lineage.kms_adapter_key_path")
        return FileBasedKMSAdapter(Path(key_path), kid=kid)
    if resolved == "env":
        if not env_var:
            raise LineageSignerError("lineage.kms_adapter=env requires lineage.kms_adapter_env_var")
        return EnvBasedKMSAdapter(env_var, kid=kid)
    if resolved == "hsm":
        if not token_uri:
            raise LineageSignerError("lineage.kms_adapter=hsm requires lineage.kms_adapter_token_uri")
        # Fail-fast at construction so a misconfigured `kms_adapter: hsm`
        # surfaces at config-load time -- not at the first audit-emit /
        # lineage-sign call deep inside the orchestrator loop. The base
        # ``HSMKMSAdapter`` is a documentation stub; a real integration
        # is delivered as a subclass that overrides ``sign`` and
        # ``public_key_jwk``.
        subclass = _resolve_hsm_subclass()
        if subclass is not None:
            return subclass(token_uri, kid=kid)
        if not _hsm_stub_opt_in():
            raise LineageSignerError(
                "lineage.kms_adapter=hsm resolves to HSMKMSAdapter, which is a "
                "documentation stub that raises NotImplementedError on sign(). "
                "Either (a) import a subclass that overrides sign() and "
                "public_key_jwk() before config load (see "
                "bernstein.core.security.lineage_kms module docstring for the "
                "PKCS#11 / Cloud-KMS integration shape), or (b) set "
                "BERNSTEIN_ALLOW_HSM_STUB=1 to opt in to the stub for "
                "non-production smoke tests.",
            )
        return HSMKMSAdapter(token_uri, kid=kid)
    raise LineageSignerError(
        f"unsupported lineage.kms_adapter={kind!r} (expected 'file', 'env', or 'hsm')",
    )


def _hsm_stub_opt_in() -> bool:
    """Return ``True`` when the operator explicitly opted into the stub.

    The opt-in flag exists so a non-production smoke test can still
    boot with ``kms_adapter: hsm`` (and crash at the first sign call,
    matching pre-fix behaviour) without a real HSM subclass on the
    classpath. Any truthy value (``1``, ``true``, ``yes``, ``on``,
    case-insensitive) counts as opted in.
    """
    raw = os.environ.get("BERNSTEIN_ALLOW_HSM_STUB", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_hsm_subclass() -> type[HSMKMSAdapter] | None:
    """Return the concrete ``HSMKMSAdapter`` subclass to use, if any.

    A customer integration ships as a subclass of :class:`HSMKMSAdapter`
    that overrides :meth:`sign` and :meth:`public_key_jwk`. If exactly
    one such subclass has been imported by the time config dispatch
    runs, return it -- the dispatcher will instantiate it instead of
    the stub. Multiple subclasses are an ambiguity the operator must
    resolve (we raise to avoid silently picking the wrong vendor).
    """
    subclasses = [
        sc
        for sc in HSMKMSAdapter.__subclasses__()
        if sc.sign is not HSMKMSAdapter.sign and sc.public_key_jwk is not HSMKMSAdapter.public_key_jwk
    ]
    if not subclasses:
        return None
    if len(subclasses) > 1:
        names = ", ".join(sorted(sc.__qualname__ for sc in subclasses))
        raise LineageSignerError(
            f"lineage.kms_adapter=hsm resolved to multiple HSMKMSAdapter "
            f"subclasses ({names}); only one HSM integration may be imported "
            f"at a time.",
        )
    return subclasses[0]


__all__ = [
    "EnvBasedKMSAdapter",
    "FileBasedKMSAdapter",
    "HSMKMSAdapter",
    "KMSAdapter",
    "kms_adapter_from_config",
]
