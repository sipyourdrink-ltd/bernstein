"""Customer-key signing layer for lineage records (schema v2).

Bernstein already chains every WAL entry with an HMAC, so the
orchestrator can self-verify a run hasn't been tampered with. That
chain is signed by Bernstein, not the customer -- a sovereign auditor
who refuses to trust upstream signing keys can't validate it.

This module adds a second, independent signature that the *customer*
controls. A :class:`LineageSigner` is plugged into
:class:`bernstein.core.persistence.lineage.LineageWriter`; every record
emitted while the signer is set carries a detached signature over the
canonicalised record bytes (see ``canonical_record_bytes``). The
signature is base64-encoded into ``LineageRecord.customer_signature``
so it round-trips through the WAL without binary escaping.

Default implementation
----------------------
:class:`Ed25519FileKeySigner` reads a customer-provided Ed25519 private
key from disk in either PEM or raw 32-byte form. We pick Ed25519 by
default because (a) signatures are 64 bytes - small enough to embed in
every WAL line, (b) signing latency is ~50µs on commodity hardware,
(c) the key format is unambiguous, (d) ``cryptography`` already ships
in Bernstein's dependency closure.

Pluggable backends
------------------
The :class:`LineageSigner` protocol is intentionally narrow: a
``sign(bytes) -> bytes`` call. HSM, TPM, or KMS-backed signers
implement the same protocol - the writer doesn't care where the key
material lives, only that the call returns a signature over the
provided canonical bytes. Verifiers are similarly pluggable via
:class:`LineageVerifier`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


@runtime_checkable
class LineageSigner(Protocol):
    """Anything that can sign canonicalised lineage record bytes."""

    def sign(self, payload: bytes) -> bytes:
        """Return a detached signature over *payload*."""
        ...


@runtime_checkable
class LineageVerifier(Protocol):
    """Anything that can verify a detached signature."""

    def verify(self, payload: bytes, signature: bytes) -> bool:
        """Return ``True`` iff *signature* is valid for *payload*."""
        ...


class LineageSignerError(RuntimeError):
    """Raised for unrecoverable signer setup or operation errors."""


class Ed25519FileKeySigner:
    """Ed25519 signer that reads a customer's private key from disk.

    Accepts either a PEM-encoded ``PRIVATE KEY`` block (PKCS#8) or a
    raw 32-byte seed. Pick PEM for human-managed keys, raw for keys
    materialised from a KMS/HSM export. The key is loaded eagerly at
    construction so a missing/corrupt key fails fast instead of at
    first emit.
    """

    __slots__ = ("_private_key", "key_path")

    def __init__(self, key_path: Path, private_key: Ed25519PrivateKey) -> None:
        self.key_path = key_path
        self._private_key = private_key

    @classmethod
    def from_path(cls, key_path: Path) -> Ed25519FileKeySigner:
        if not key_path.exists():
            raise LineageSignerError(f"signing key not found: {key_path}")
        try:
            data = key_path.read_bytes()
        except OSError as exc:
            raise LineageSignerError(f"cannot read signing key {key_path}: {exc}") from exc
        return cls(key_path, _load_ed25519_private(data, key_path))

    def sign(self, payload: bytes) -> bytes:
        return self._private_key.sign(payload)

    def public_key_bytes(self) -> bytes:
        """Return the raw 32-byte public key (for handing to the auditor)."""
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_key_jwk(self) -> dict[str, str]:
        """Return the verifying key as an RFC 7517 JWK.

        Every :class:`~bernstein.core.security.key_custody.KMSAdapter`
        advertises its key this way, and :func:`signer_from_config` can hand
        back either one of those or this class depending on which config
        shape the operator wrote. Without this method the same key on the
        same disk exposed a different surface depending on whether it was
        reached through ``key_path=`` or through ``kms_adapter='file'``, and
        an auditor's JWK-based attestation flow worked or broke on that
        distinction alone.

        Delegates to the same encoder the adapters use, so the two paths
        cannot drift into disagreeing about the encoding: RFC 8037 CFRG
        curves, ``kty='OKP'``, ``crv='Ed25519'``, ``x`` base64url-no-pad.

        Returns:
            The JWK, with ``kid`` set to the key file's name so an auditor
            handed several keys can tell them apart.
        """
        from bernstein.core.security.key_custody import public_key_jwk_for

        return public_key_jwk_for(self._private_key.public_key(), kid=self.key_path.name)


class HSMSigner:
    """Named stub for an HSM-backed lineage signer. Signing always raises.

    This exists so ``key_kind='hsm'`` has something to *point at*. The
    Phase-1 dispatcher used to reject it with the same generic
    "unsupported key_kind" error it gives a typo, which left an operator
    unable to tell "HSM is not implemented yet" from "HSM is refused on
    purpose". Those warrant different next steps.

    The real integration shape lives in
    :class:`~bernstein.core.security.key_custody.HSMKMSAdapter` - PKCS#11
    token URI, ``C_Login`` with an operator-supplied PIN, ``C_Sign`` over an
    Ed25519 key handle - and is delivered by subclassing *that* class and
    selecting it with ``kms_adapter='hsm'``.

    Documentation only. Nothing in ``src/`` constructs one and no dispatcher
    returns one - ``signer_from_config`` raises for ``key_kind='hsm'`` rather
    than handing this back, because an object that explodes on the first WAL
    emit is a worse outcome than a startup error. It exists so the error above
    has something to name, and so the integration shape has a home.

    Deliberately **not** a subclass of ``HSMKMSAdapter``.
    ``_resolve_hsm_subclass`` discovers a customer's integration through
    ``HSMKMSAdapter.__subclasses__()``, which returns direct subclasses only.
    A customer who subclassed this class instead would be invisible to that
    lookup and would silently fall back to the stub. Being a peer that
    satisfies :class:`LineageSigner` structurally keeps that discovery
    working and costs nothing - ``LineageSigner`` is a Protocol.
    """

    __slots__ = ()

    def sign(self, payload: bytes) -> bytes:
        """Always raise: there is no HSM behind this object.

        Raises:
            NotImplementedError: Always.
        """
        del payload  # named stub: never actually used
        raise NotImplementedError(
            "HSMSigner is a named stub, not an HSM client. Bernstein ships no "
            "PKCS#11 / Cloud-KMS integration because the token layout, PIN "
            "delivery and FIPS mode are customer-specific. Deliver yours as a "
            "subclass of bernstein.core.security.key_custody.HSMKMSAdapter "
            "that overrides sign() and public_key_jwk(), import it before "
            "config load, and select it with lineage.kms_adapter='hsm'.",
        )


class Ed25519PublicKeyVerifier:
    """Verifier paired with :class:`Ed25519FileKeySigner`.

    Constructed from either a raw 32-byte public key (e.g. material
    handed to the customer auditor out-of-band) or a PEM-encoded
    public key on disk.
    """

    __slots__ = ("_public_key",)

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._public_key = public_key

    @classmethod
    def from_raw(cls, raw: bytes) -> Ed25519PublicKeyVerifier:
        if len(raw) != 32:
            raise LineageSignerError(f"raw Ed25519 public key must be 32 bytes, got {len(raw)}")
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    @classmethod
    def from_path(cls, key_path: Path) -> Ed25519PublicKeyVerifier:
        if not key_path.exists():
            raise LineageSignerError(f"public key not found: {key_path}")
        data = key_path.read_bytes()
        if data.lstrip().startswith(b"-----BEGIN"):
            try:
                public_key = serialization.load_pem_public_key(data)
            except ValueError as exc:
                raise LineageSignerError(f"invalid PEM public key {key_path}: {exc}") from exc
            if not isinstance(public_key, Ed25519PublicKey):
                raise LineageSignerError(f"public key {key_path} is not Ed25519")
            return cls(public_key)
        return cls.from_raw(data.strip() if len(data) > 32 else data)

    def verify(self, payload: bytes, signature: bytes) -> bool:
        try:
            self._public_key.verify(signature, payload)
        except InvalidSignature:
            return False
        return True


def _load_ed25519_private(data: bytes, source: Path) -> Ed25519PrivateKey:
    if data.lstrip().startswith(b"-----BEGIN"):
        try:
            private_key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise LineageSignerError(f"invalid PEM private key {source}: {exc}") from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise LineageSignerError(f"private key {source} is not Ed25519")
        return private_key
    raw = data.strip() if len(data) > 32 else data
    if len(raw) != 32:
        raise LineageSignerError(
            f"raw Ed25519 private key must be 32 bytes (got {len(raw)} from {source})",
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise LineageSignerError(f"cannot load raw Ed25519 key from {source}: {exc}") from exc


# ---------------------------------------------------------------------------
# Attachment-as-parent helper (issue #1797)
# ---------------------------------------------------------------------------
# Additive, append-only surface: the lineage receipt for any artefact
# produced by a worker this turn must carry the input attachment's
# SHA-256 in its parents list. The helper below builds the canonical
# parent identifier from an attachment digest so that callers do not
# have to know the URI format.

_ATTACHMENT_PARENT_SCHEME = "multimodal-attachment://"
_HEX_DIGIT_SET = frozenset("0123456789abcdef")


def build_attachment_parent_uri(sha256: str) -> str:
    """Return the canonical parent URI for a multimodal attachment.

    Args:
        sha256: Hex digest of the attachment bytes (lower-case, 64 chars).

    Returns:
        A scheme-qualified content-addressed URI suitable for inclusion
        in a lineage record's ``parents`` list.

    Raises:
        LineageSignerError: When *sha256* is not exactly 64 lower-case
            hexadecimal characters. (bot-ack: 3284182781 --
            CodeRabbit major.)
    """
    if not sha256 or len(sha256) != 64:
        raise LineageSignerError(f"attachment sha256 must be 64 hex chars, got {len(sha256)}")
    if not _HEX_DIGIT_SET.issuperset(sha256):
        raise LineageSignerError("attachment sha256 must be lower-case hex (0-9a-f)")
    return f"{_ATTACHMENT_PARENT_SCHEME}{sha256}"


def register_attachment_parents(
    parents: list[str],
    attachment_sha256s: list[str],
) -> list[str]:
    """Append attachment parent URIs to an existing lineage parents list.

    The function never mutates *parents*; it returns a new list with
    the existing parents followed by the attachment-derived URIs.
    Duplicate entries are filtered out so a multiply-attached image
    appears exactly once in the receipt.

    Args:
        parents: The existing lineage parents list.
        attachment_sha256s: Hex digests of attachments to register.

    Returns:
        A new list with attachment parents appended.
    """
    seen: set[str] = set(parents)
    out: list[str] = parents.copy()
    for digest in attachment_sha256s:
        uri = build_attachment_parent_uri(digest)
        if uri not in seen:
            out.append(uri)
            seen.add(uri)
    return out


def signer_from_config(
    *,
    enabled: bool,
    key_path: str | None = None,
    key_kind: str = "ed25519",
    kms_adapter: str | None = None,
    kms_env_var: str | None = None,
    kms_token_uri: str | None = None,
    kms_kid: str | None = None,
) -> LineageSigner | None:
    """Build a :class:`LineageSigner` from bernstein.yaml-shaped config.

    Two configuration shapes are supported:

    * **Phase-1 file-only** (back-compat): ``enabled=True`` +
      ``key_path=...`` reads an Ed25519 PEM/raw key off disk. Equivalent
      to ``kms_adapter='file'`` + ``key_path=...``.
    * **Phase-2 KMS-pluggable**: ``kms_adapter='file'|'env'|'hsm'``
      dispatches to the matching ``KMSAdapter`` from
      :mod:`bernstein.core.security.lineage_kms`. The HSM adapter is a
      documented stub (raises ``NotImplementedError``) so the wiring is
      in place even when the customer integration isn't.

    Returns ``None`` when signing is disabled or unconfigured. Raises
    :class:`LineageSignerError` when ``enabled=True`` but the key cannot
    be loaded - the orchestrator should fail fast rather than silently
    drop signatures.
    """
    if not enabled:
        return None
    # Phase-2 path: explicit kms_adapter selector.
    if kms_adapter is not None:
        # Imported lazily so this module stays free of the security
        # package import cycle (lineage_kms imports back from here).
        from bernstein.core.security.lineage_kms import kms_adapter_from_config

        return kms_adapter_from_config(
            enabled=True,
            kind=kms_adapter,
            key_path=key_path,
            env_var=kms_env_var,
            token_uri=kms_token_uri,
            kid=kms_kid,
        )
    # Phase-1 path: file key by default. ``key_kind`` is normalised once -
    # the two checks below used to compare it differently, so ``key_kind: HSM``
    # took the typo branch while ``hsm`` took the other.
    kind = key_kind.lower().strip()
    # Before the key_path guard, deliberately. An operator choosing an HSM is
    # by definition not pointing at a private key file on disk, so they set no
    # key_path - and answering "requires key_path" sends them to create the
    # file key they were trying to avoid. Ordered the other way, the message
    # only reached someone who set key_kind='hsm' *and* a key_path, which is a
    # config nobody writes on purpose.
    if kind == "hsm":
        raise LineageSignerError(
            "lineage.customer_signing.key_kind='hsm' is not implemented on the Phase-1 "
            "key_path route: HSMSigner is a named stub whose sign() raises. Deliver an "
            "integration as a subclass of "
            "bernstein.core.security.key_custody.HSMKMSAdapter and select it with "
            "lineage.kms_adapter='hsm' (plus lineage.kms_adapter_token_uri), which is the "
            "Phase-2 route that dispatches to it.",
        )
    if kind != "ed25519":
        raise LineageSignerError(
            f"unsupported lineage.customer_signing.key_kind: {key_kind!r} (only 'ed25519' is implemented in Phase 1)",
        )
    if key_path is None:
        raise LineageSignerError("lineage.customer_signing.enabled=true requires key_path")
    return Ed25519FileKeySigner.from_path(Path(key_path))
