"""Attenuated delegation capability tokens for verifiable multi-hop authority.

When a run fans out, authority fans out with it. This module makes each
delegation hop a **signed, scope-attenuating capability token** so the
``principal -> orchestrator -> sub-agent`` authority chain becomes a single
offline-verifiable structure: an auditor asking "who authorized this sub-agent
to write these files, and was that authority ever broader than the human
granted?" gets a cryptographic answer from the token bytes alone, with no live
coordinator, no registry, and no network.

AUTHORITY tokens vs. the ACT log
--------------------------------
This module records *authority* - the scoped, signed grant that flows down a
delegation chain. It is deliberately distinct from
:mod:`bernstein.core.identity.delegation`, which records per-hop HMAC
*receipts* (the ACT log: which principal authorized which sub-agent action for
a run). Receipts may later reference token hashes, but the two surfaces are not
coupled here.

Wire format
-----------
Each :class:`CapabilityToken` is signed with a **detached JWS** (RFC 7515 §A.5)
over the **JCS-canonical** (RFC 8785) token body, using the shared Ed25519
primitives in :mod:`bernstein.core.security.agent_card_signer`. The JWS carries
a token-specific ``typ`` header (``delegation-capability+jws``) so a signature
minted for an agent card cannot be replayed as a token, and vice versa. The
token binds its own ``issuer_pubkey`` and its delegatee's ``subject_pubkey``,
so verification checks against the key captured *at mint time* - **key rotation
never invalidates historical tokens**.

Attenuation (tokens narrow, they never grant)
---------------------------------------------
:func:`attenuate` mints a child token and enforces that the child's caveats are
a **subset** of the parent's over every axis:

* ``permissions`` - set-subset over the ``PERM_*`` vocabulary.
* ``task_ids`` - allowlist subset (``None`` means unconstrained/widest).
* ``path_prefixes`` - POSIX-normalized prefix coverage; a child prefix is
  covered iff some parent prefix is an ancestor-or-equal of it (so ``/a/b``
  covers ``/a/b/c`` and ``/a/b`` but never ``/a/bc``).
* ``not_after`` - expiry no later than the parent.
* ``max_uses`` - no greater than the parent (``None`` means unlimited/widest).
* ``remaining_depth`` - **strictly less** than the parent (the ``max_depth``
  caveat). A chain cannot grow past the depth its root authorized; a hop whose
  depth is not strictly below its parent's is rejected at mint *and* at verify.

Widening at any hop is rejected at mint time (:class:`AttenuationError`) and is
independently caught at verify time (:func:`verify_chain`), so a re-signed,
structurally-continuous but widened hop still fails from the signed bytes alone.

Two-tier boundary
------------------
Tokens express only *narrowing*. They cannot **widen** authority and they
cannot express **approval-gated actions** (an action that must route through a
human/approval step). Approval-gated escalation is intentionally *not*
representable as a caveat: the approval-receipt surface stays the escalation
path. A capability token answers "was this sub-agent ever granted more than its
parent held?"; it never answers "may this sub-agent perform an action that
requires fresh approval?" - that second question belongs to the approval
receipts, by design.

RFC 8693 projection
-------------------
:func:`to_actor_claims` renders a *verified* chain as nested RFC 8693 ``act``
claims (``{"sub": ..., "act": {"sub": ..., "act": {...}}}``) so standard IdP
tooling can consume the delegation path without understanding this token
format. The projection refuses an unverified chain.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import posixpath
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.agents.agent_identity import (
    PERM_AGENTS_READ,
    PERM_AGENTS_SPAWN,
    PERM_CONFIG_READ,
    PERM_FILES_READ,
    PERM_FILES_WRITE,
    PERM_STATUS_READ,
    PERM_TASKS_CLAIM,
    PERM_TASKS_READ,
    PERM_TASKS_WRITE,
    PERM_TESTS_RUN,
)
from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    sign_detached_jws_over_canonical,
    verify_detached_jws_over_canonical,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "GENESIS_PARENT",
    "TOKEN_TYP",
    "AttenuationError",
    "CapabilityChain",
    "CapabilityToken",
    "Caveats",
    "ChainVerification",
    "HopVerification",
    "TokenVerificationError",
    "allowlist_narrows",
    "attenuate",
    "bound_narrows",
    "caveats_for_scope",
    "mint_root",
    "narrowing_violations",
    "path_covered_by",
    "prefixes_narrow",
    "scope_permissions",
    "sign_token",
    "to_actor_claims",
    "uses_narrows",
    "verify_chain",
]

#: JWS ``typ`` header binding a signature to the capability-token context. A
#: signature minted with any other ``typ`` (an agent card, a lineage record)
#: will not verify as a token, and vice versa.
TOKEN_TYP: str = "delegation-capability+jws"

#: Parent-hash sentinel for a root token (the genesis of a chain). Matches the
#: 64-hex-zero anchor convention used by the audit and delegation-receipt chains.
GENESIS_PARENT: str = "0" * 64


class AttenuationError(ValueError):
    """A child token attempted to widen (not narrow) its parent's authority."""


class TokenVerificationError(ValueError):
    """An operation required a verified chain but verification failed."""


# ---------------------------------------------------------------------------
# Enum -> caveat mapping (back-compat bridge for scope-based callers)
# ---------------------------------------------------------------------------

#: Cumulative permission sets for the legacy ``DelegationScope`` enum, expressed
#: over the ``PERM_*`` vocabulary. ``read`` < ``write`` < ``execute`` < ``full``
#: so a token minted for a narrower scope is a strict subset of a wider one -
#: the enum hierarchy maps onto capability-token subset attenuation unchanged.
_SCOPE_READ: frozenset[str] = frozenset(
    {PERM_TASKS_READ, PERM_FILES_READ, PERM_STATUS_READ, PERM_AGENTS_READ, PERM_CONFIG_READ}
)
_SCOPE_WRITE: frozenset[str] = _SCOPE_READ | {PERM_TASKS_WRITE, PERM_FILES_WRITE, PERM_TASKS_CLAIM}
_SCOPE_EXECUTE: frozenset[str] = _SCOPE_WRITE | {PERM_TESTS_RUN, PERM_AGENTS_SPAWN}
_SCOPE_FULL: frozenset[str] = _SCOPE_EXECUTE

_SCOPE_PERMISSIONS: dict[str, frozenset[str]] = {
    "read": _SCOPE_READ,
    "write": _SCOPE_WRITE,
    "execute": _SCOPE_EXECUTE,
    "full": _SCOPE_FULL,
}


def scope_permissions(scope: str) -> frozenset[str]:
    """Return the ``PERM_*`` set a legacy ``DelegationScope`` enum maps onto."""
    return _SCOPE_PERMISSIONS.get(scope, _SCOPE_READ)


def caveats_for_scope(
    scope: str,
    *,
    remaining_depth: int,
    not_after: float,
    task_ids: set[str] | frozenset[str] | None = None,
    path_prefixes: set[str] | frozenset[str] | None = None,
    max_uses: int | None = None,
    extra_permissions: set[str] | frozenset[str] | None = None,
) -> Caveats:
    """Build a :class:`Caveats` from a legacy scope enum (enum -> caveat bridge).

    Existing ``permission_delegation`` callers speak the coarse
    ``read``/``write``/``execute``/``full`` enum; this renders that scope as the
    explicit caveat set the signed path enforces, so those callers keep working.
    """
    perms = set(scope_permissions(scope))
    if extra_permissions:
        perms |= set(extra_permissions)
    return Caveats(
        permissions=frozenset(perms),
        remaining_depth=remaining_depth,
        not_after=not_after,
        task_ids=frozenset(task_ids) if task_ids is not None else None,
        path_prefixes=(frozenset(_normalize_path(p) for p in path_prefixes) if path_prefixes is not None else None),
        max_uses=max_uses,
    )


# ---------------------------------------------------------------------------
# Path-prefix subset semantics (POSIX-normalized ancestor-or-equal coverage)
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """Return the POSIX-normalized form of *path* (collapse ``.``/``..``/``//``)."""
    return posixpath.normpath(path)


def path_covered_by(child: str, parent: str) -> bool:
    """Return True iff *parent* is an ancestor-or-equal of *child* (POSIX).

    Coverage is component-wise, not string-prefix: ``/a/b`` covers ``/a/b`` and
    ``/a/b/c`` but not ``/a/bc``. Both operands are normalized first, so
    ``/a/./b`` and ``/a/b/`` compare equal to ``/a/b``.
    """
    c = _normalize_path(child)
    p = _normalize_path(parent)
    if p == c:
        return True
    boundary = p if p.endswith("/") else p + "/"
    return c.startswith(boundary)


def allowlist_narrows(child: frozenset[str] | None, parent: frozenset[str] | None) -> bool:
    """Subset for allowlists where ``None`` is the universal (widest) set."""
    if parent is None:
        return True
    if child is None:
        return False
    return child <= parent


def prefixes_narrow(child: frozenset[str] | None, parent: frozenset[str] | None) -> bool:
    """Every child prefix must be covered by some parent prefix (``None`` = all)."""
    if parent is None:
        return True
    if child is None:
        return False
    return all(any(path_covered_by(c, p) for p in parent) for c in child)


def uses_narrows(child: int | None, parent: int | None) -> bool:
    """``max_uses`` subset where ``None`` means unlimited (widest)."""
    if parent is None:
        return True
    if child is None:
        return False
    return child <= parent


def bound_narrows(child: float | int | None, parent: float | int | None) -> bool:
    """Upper-bound subset where ``None`` means unbounded (widest).

    Used for ceilings such as ``not_after`` and depth budgets: the child may
    only carry a bound no greater than its parent's, and may not drop a bound
    the parent imposed.
    """
    if parent is None:
        return True
    if child is None:
        return False
    return child <= parent


#: Private aliases retained so the module body reads the same as before the
#: primitives were promoted to the public surface for reuse by the
#: delegation-receipt narrowing verifier.
_path_covered_by = path_covered_by
_allowlist_narrows = allowlist_narrows
_prefixes_narrow = prefixes_narrow
_uses_narrows = uses_narrows


def narrowing_violations(child: Caveats, parent: Caveats) -> tuple[str, ...]:
    """Return the caveat axes on which ``child`` widens ``parent``.

    An empty tuple means the child is a strict attenuation of the parent. The
    axis names are stable identifiers (``permissions``, ``task_ids``,
    ``path_prefixes``, ``not_after``, ``max_uses``, ``remaining_depth``) so a
    verifier can name the offending axis rather than reporting a bare
    pass/fail.
    """
    axes: list[str] = []
    if not child.permissions <= parent.permissions:
        axes.append("permissions")
    if not allowlist_narrows(child.task_ids, parent.task_ids):
        axes.append("task_ids")
    if not prefixes_narrow(child.path_prefixes, parent.path_prefixes):
        axes.append("path_prefixes")
    if child.not_after > parent.not_after:
        axes.append("not_after")
    if not uses_narrows(child.max_uses, parent.max_uses):
        axes.append("max_uses")
    if not 0 <= child.remaining_depth < parent.remaining_depth:
        axes.append("remaining_depth")
    return tuple(axes)


# ---------------------------------------------------------------------------
# Caveats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caveats:
    """The scope-narrowing predicates a capability token carries.

    A child token's caveats must be a subset of its parent's over every field
    (see :meth:`is_narrowing_of`). ``task_ids`` and ``path_prefixes`` use
    ``None`` to mean *unconstrained* (the widest possible value); ``max_uses``
    uses ``None`` to mean *unlimited*.
    """

    permissions: frozenset[str]
    remaining_depth: int
    not_after: float
    task_ids: frozenset[str] | None = None
    path_prefixes: frozenset[str] | None = None
    max_uses: int | None = None

    def is_narrowing_of(self, parent: Caveats) -> bool:
        """Return True iff ``self`` narrows (is a subset of) ``parent``.

        Enforces set-subset over permissions and task-ids, ancestor-or-equal
        prefix coverage, ``not_after <=``, ``max_uses <=``, and the
        ``max_depth`` rule ``0 <= remaining_depth < parent.remaining_depth``.
        Delegates to :func:`narrowing_violations` so the boolean verdict and
        the per-axis diagnosis can never disagree.
        """
        return not narrowing_violations(self, parent)

    def to_body(self) -> dict[str, Any]:
        """Return a JSON/JCS-ready dict (sets rendered as sorted lists)."""
        return {
            "permissions": sorted(self.permissions),
            "remaining_depth": self.remaining_depth,
            "not_after": self.not_after,
            "task_ids": sorted(self.task_ids) if self.task_ids is not None else None,
            "path_prefixes": sorted(self.path_prefixes) if self.path_prefixes is not None else None,
            "max_uses": self.max_uses,
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Caveats:
        """Inverse of :meth:`to_body`."""
        task_ids = body.get("task_ids")
        prefixes = body.get("path_prefixes")
        return cls(
            permissions=frozenset(body["permissions"]),
            remaining_depth=int(body["remaining_depth"]),
            not_after=float(body["not_after"]),
            task_ids=frozenset(task_ids) if task_ids is not None else None,
            path_prefixes=frozenset(prefixes) if prefixes is not None else None,
            max_uses=body.get("max_uses"),
        )


# ---------------------------------------------------------------------------
# CapabilityToken
# ---------------------------------------------------------------------------


def _spki_pem(private_key_pem: bytes) -> str:
    """Return the SPKI-PEM public key text derived from an Ed25519 private PEM."""
    from cryptography.hazmat.primitives import serialization

    priv = serialization.load_pem_private_key(private_key_pem, password=None)
    return (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


def _raw_pub(pem: str | bytes) -> bytes:
    """Return the raw 32-byte Ed25519 public key for a SPKI-PEM (for comparison).

    Comparing the raw key bytes rather than PEM text makes continuity and
    trust-anchor checks insensitive to PEM whitespace/line-ending differences.
    Returns ``b""`` for input that is not a loadable Ed25519 public key.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    data = pem.encode("ascii") if isinstance(pem, str) else pem
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError):
        return b""
    if not isinstance(key, Ed25519PublicKey):
        return b""
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass(frozen=True)
class CapabilityToken:
    """One signed, scope-attenuating hop of a delegation authority chain."""

    token_id: str
    issuer_identity_id: str
    issuer_pubkey: str  # SPKI-PEM text of the delegating identity's Ed25519 key
    subject_identity_id: str
    subject_pubkey: str  # SPKI-PEM text of the delegatee's Ed25519 key
    caveats: Caveats
    parent_token_hash: str
    audit_head: str
    granted_at: float
    jws: str  # detached JWS (RFC 7515 §A.5) over canonicalize_jcs(self.body())

    def body(self) -> dict[str, Any]:
        """Return the signed token body (every field except ``jws``).

        This exact dict is JCS-canonicalized to form the JWS signing input and
        the token hash, so any change to a signed field breaks both.
        """
        return {
            "audit_head": self.audit_head,
            "caveats": self.caveats.to_body(),
            "granted_at": self.granted_at,
            "issuer_identity_id": self.issuer_identity_id,
            "issuer_pubkey": self.issuer_pubkey,
            "parent_token_hash": self.parent_token_hash,
            "subject_identity_id": self.subject_identity_id,
            "subject_pubkey": self.subject_pubkey,
            "token_id": self.token_id,
        }

    def token_hash(self) -> str:
        """Return the SHA-256 of the JCS-canonical body (this token's identity)."""
        return hashlib.sha256(canonicalize_jcs(self.body())).hexdigest()

    def verify_signature(self) -> bool:
        """Return True iff the detached JWS verifies against ``issuer_pubkey``."""
        return verify_detached_jws_over_canonical(
            canonicalize_jcs(self.body()),
            self.jws,
            self.issuer_pubkey.encode("ascii"),
            expected_typ=TOKEN_TYP,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (signed body plus the ``jws``)."""
        return {**self.body(), "jws": self.jws}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityToken:
        """Inverse of :meth:`to_dict`."""
        return cls(
            token_id=data["token_id"],
            issuer_identity_id=data["issuer_identity_id"],
            issuer_pubkey=data["issuer_pubkey"],
            subject_identity_id=data["subject_identity_id"],
            subject_pubkey=data["subject_pubkey"],
            caveats=Caveats.from_body(data["caveats"]),
            parent_token_hash=data["parent_token_hash"],
            audit_head=data["audit_head"],
            granted_at=float(data["granted_at"]),
            jws=data["jws"],
        )


@dataclass(frozen=True)
class CapabilityChain:
    """An ordered delegation chain, root first, leaf last."""

    tokens: tuple[CapabilityToken, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"tokens": [t.to_dict() for t in self.tokens]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityChain:
        return cls(tokens=tuple(CapabilityToken.from_dict(t) for t in data["tokens"]))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, blob: str) -> CapabilityChain:
        return cls.from_dict(json.loads(blob))


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def sign_token(
    *,
    token_id: str,
    issuer_identity_id: str,
    issuer_private_key: bytes,
    subject_identity_id: str,
    subject_pubkey: bytes | str,
    caveats: Caveats,
    parent_token_hash: str,
    audit_head: str,
    granted_at: float,
) -> CapabilityToken:
    """Sign a token body with ``issuer_private_key`` (no attenuation check).

    Low-level constructor: :func:`mint_root` and :func:`attenuate` layer the
    trust-anchor and subset rules on top. Exposed so tests can forge a
    validly-signed-but-widened hop and prove :func:`verify_chain` rejects it on
    attenuation alone.
    """
    issuer_pubkey = _spki_pem(issuer_private_key)
    subject_pem = subject_pubkey.decode("ascii") if isinstance(subject_pubkey, bytes) else subject_pubkey
    token = CapabilityToken(
        token_id=token_id,
        issuer_identity_id=issuer_identity_id,
        issuer_pubkey=issuer_pubkey,
        subject_identity_id=subject_identity_id,
        subject_pubkey=subject_pem,
        caveats=caveats,
        parent_token_hash=parent_token_hash,
        audit_head=audit_head,
        granted_at=granted_at,
        jws="",
    )
    jws = sign_detached_jws_over_canonical(
        canonicalize_jcs(token.body()),
        issuer_private_key,
        typ=TOKEN_TYP,
        kid=issuer_identity_id,
    )
    from dataclasses import replace

    return replace(token, jws=jws)


def _anchor_and_head(
    audit_chain: AuditChainStore | None,
    default_head: str,
) -> str:
    """Return the audit head to bind into a mint (the chain tip, or default).

    Read from disk, not from the store's cache, and only meaningful inside the
    :meth:`AuditChainStore.chain_transaction` that also records the mint: the
    head is signed into the token and ``_audit_head_matches`` compares it back
    against the mint record, so a head that moves in between turns a valid token
    into one that fails its own anchor check.
    """
    if audit_chain is None:
        return default_head
    return audit_chain.resync_head()


@contextlib.contextmanager
def _mint_section(audit_chain: AuditChainStore | None) -> Iterator[None]:
    """Hold the chain across capture-head -> sign -> record, when one is wired."""
    if audit_chain is None:
        yield
        return
    with audit_chain.chain_transaction():
        yield


def _record_mint(audit_chain: AuditChainStore | None, token: CapabilityToken) -> AuditEvent | None:
    """Emit a ``delegation_minted`` event cross-referencing the token, if wired."""
    if audit_chain is None:
        return None
    from bernstein.core.security.audit_chain import record_delegation_minted

    return record_delegation_minted(
        chain=audit_chain,
        token_hash=token.token_hash(),
        issuer_identity_id=token.issuer_identity_id,
        subject_identity_id=token.subject_identity_id,
        parent_token_hash=token.parent_token_hash,
        remaining_depth=token.caveats.remaining_depth,
    )


def mint_root(
    *,
    issuer_identity_id: str,
    issuer_private_key: bytes,
    subject_identity_id: str,
    subject_pubkey: bytes | str,
    caveats: Caveats,
    audit_head: str = GENESIS_PARENT,
    granted_at: float | None = None,
    token_id: str | None = None,
    audit_chain: AuditChainStore | None = None,
) -> CapabilityToken:
    """Mint a root capability token (the principal's grant to the orchestrator).

    The root's issuer is the human principal's install identity; a verifier only
    accepts a chain whose root ``issuer_pubkey`` is a configured trust anchor.

    When ``audit_chain`` is supplied, the mint is anchored: ``audit_head`` is
    bound to the chain tip captured before the mint and a ``delegation_minted``
    audit event is emitted whose ``token_hash`` and ``prev_chain_digest``
    cross-reference this token.
    """
    with _mint_section(audit_chain):
        head = _anchor_and_head(audit_chain, audit_head)
        token = sign_token(
            token_id=token_id or uuid.uuid4().hex,
            issuer_identity_id=issuer_identity_id,
            issuer_private_key=issuer_private_key,
            subject_identity_id=subject_identity_id,
            subject_pubkey=subject_pubkey,
            caveats=caveats,
            parent_token_hash=GENESIS_PARENT,
            audit_head=head,
            granted_at=granted_at if granted_at is not None else time.time(),
        )
        _record_mint(audit_chain, token)
    return token


def attenuate(
    parent: CapabilityToken,
    *,
    issuer_private_key: bytes,
    subject_identity_id: str,
    subject_pubkey: bytes | str,
    caveats: Caveats,
    audit_head: str | None = None,
    granted_at: float | None = None,
    token_id: str | None = None,
    audit_chain: AuditChainStore | None = None,
) -> CapabilityToken:
    """Mint a child token that narrows ``parent`` (tokens narrow, never grant).

    The child's issuer *is* the parent's subject: ``issuer_private_key`` must
    correspond to ``parent.subject_pubkey`` (the party that was delegated to now
    delegates onward). Widening on any caveat axis - including a
    ``remaining_depth`` that is not strictly below the parent's - raises
    :class:`AttenuationError`.
    """
    if _raw_pub(_spki_pem(issuer_private_key)) != _raw_pub(parent.subject_pubkey):
        raise AttenuationError(
            "issuer_private_key does not match the parent token's subject_pubkey; "
            "only the delegatee of the parent hop may attenuate onward"
        )
    if not caveats.is_narrowing_of(parent.caveats):
        raise AttenuationError(
            "child caveats widen the parent (permissions, task_ids, path_prefixes, "
            "expiry, max_uses, or remaining_depth); tokens may only narrow"
        )
    with _mint_section(audit_chain):
        head = _anchor_and_head(audit_chain, audit_head if audit_head is not None else parent.audit_head)
        token = sign_token(
            token_id=token_id or uuid.uuid4().hex,
            issuer_identity_id=parent.subject_identity_id,
            issuer_private_key=issuer_private_key,
            subject_identity_id=subject_identity_id,
            subject_pubkey=subject_pubkey,
            caveats=caveats,
            parent_token_hash=parent.token_hash(),
            audit_head=head,
            granted_at=granted_at if granted_at is not None else time.time(),
        )
        _record_mint(audit_chain, token)
    return token


# ---------------------------------------------------------------------------
# Offline chain verification
# ---------------------------------------------------------------------------


@dataclass
class HopVerification:
    """Per-hop verdict from :func:`verify_chain`."""

    hop_index: int
    issuer_identity_id: str
    subject_identity_id: str
    ok: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class ChainVerification:
    """The result of walking a chain offline."""

    valid: bool
    hops: list[HopVerification] = field(default_factory=list)
    principal_path: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def verify_chain(
    chain: CapabilityChain,
    *,
    trust_anchors: set[str],
    audit_chain: AuditChainStore | None = None,
) -> ChainVerification:
    """Verify a delegation chain offline and return the resolved authority path.

    Walks the chain root -> leaf, checking at every hop: the detached JWS
    against the hop's captured ``issuer_pubkey``; structural linkage
    (``parent_token_hash`` equals the preceding hop's computed hash); identity
    and pubkey continuity (the issuer of hop N is the subject of hop N-1); and
    monotonic attenuation (each hop's caveats narrow its parent's). The root's
    ``issuer_pubkey`` must be one of ``trust_anchors`` (the principal's install
    identity).

    No network and no live registry are consulted. When ``audit_chain`` is
    supplied, each hop's ``audit_head`` is additionally checked to be an ancestor
    of the current head with a matching ``delegation_minted`` event - but the
    signature/attenuation verdict never depends on it, so tamper detection holds
    with the registry and audit log unavailable.
    """
    anchor_raws = {_raw_pub(a) for a in trust_anchors}
    anchor_raws.discard(b"")

    hops: list[HopVerification] = []
    errors: list[str] = []
    prev_hash = GENESIS_PARENT
    prev_subject_id: str | None = None
    prev_subject_pubkey: str | None = None
    prev_caveats: Caveats | None = None

    tokens = chain.tokens
    if not tokens:
        return ChainVerification(valid=False, hops=[], principal_path=[], errors=["empty chain"])

    audit_events = None
    if audit_chain is not None:
        from bernstein.core.security.audit_chain import EVENT_DELEGATION_MINTED

        audit_events = audit_chain.query(event_type=EVENT_DELEGATION_MINTED)

    for index, token in enumerate(tokens):
        hop_errors: list[str] = []

        if not token.verify_signature():
            hop_errors.append("signature invalid: JWS does not verify against issuer_pubkey")

        if token.parent_token_hash != prev_hash:
            hop_errors.append("parent hash mismatch: token does not chain onto the preceding hop")

        if index == 0:
            if _raw_pub(token.issuer_pubkey) not in anchor_raws:
                hop_errors.append("root issuer_pubkey is not a configured trust anchor")
        else:
            if token.issuer_identity_id != prev_subject_id:
                hop_errors.append("identity discontinuity: issuer is not the subject of the hop above")
            if prev_subject_pubkey is None or _raw_pub(token.issuer_pubkey) != _raw_pub(prev_subject_pubkey):
                hop_errors.append("pubkey discontinuity: issuer_pubkey is not the subject_pubkey above")
            widened = narrowing_violations(token.caveats, prev_caveats) if prev_caveats is not None else ()
            if widened:
                hop_errors.append(
                    "attenuation violated: caveats widen the parent (tokens may only narrow); "
                    f"offending axes: {', '.join(widened)}"
                )

        if audit_events is not None and not _audit_head_matches(token, audit_events):
            hop_errors.append("audit anchor missing: no delegation_minted event cross-references this token")

        hops.append(
            HopVerification(
                hop_index=index,
                issuer_identity_id=token.issuer_identity_id,
                subject_identity_id=token.subject_identity_id,
                ok=not hop_errors,
                errors=hop_errors,
            )
        )

        prev_hash = token.token_hash()
        prev_subject_id = token.subject_identity_id
        prev_subject_pubkey = token.subject_pubkey
        prev_caveats = token.caveats

    principal_path = [tokens[0].issuer_identity_id] + [t.subject_identity_id for t in tokens]
    valid = all(hop.ok for hop in hops)
    return ChainVerification(valid=valid, hops=hops, principal_path=principal_path, errors=errors)


def _audit_head_matches(token: CapabilityToken, events: list[AuditEvent]) -> bool:
    """Return True iff a ``delegation_minted`` event cross-references *token*.

    The event's ``token_hash`` must equal the token's hash and its embedded
    ``prev_chain_digest`` must equal the token's ``audit_head`` - the two-way
    cross-reference minted by :func:`_record_mint`.
    """
    want_hash = token.token_hash()
    for event in events:
        details = event.details
        if details.get("token_hash") == want_hash and details.get("prev_chain_digest") == token.audit_head:
            return True
    return False


# ---------------------------------------------------------------------------
# RFC 8693 actor-claims projection
# ---------------------------------------------------------------------------


def to_actor_claims(chain: CapabilityChain, *, trust_anchors: set[str]) -> dict[str, Any]:
    """Render a *verified* chain as nested RFC 8693 ``act`` claims.

    The outermost ``sub`` is the principal (the root issuer, the ultimate
    authority); each nested ``act`` descends through the actors that were
    delegated to, ending at the leaf subject. External IdP tooling can consume
    the delegation path without understanding the token format.

    Raises:
        TokenVerificationError: If the chain does not verify. The projection
            only speaks for an authority path it has cryptographically checked.
    """
    result = verify_chain(chain, trust_anchors=trust_anchors)
    if not result.valid:
        raise TokenVerificationError(
            "refusing to project an unverified chain as actor claims: " + "; ".join(_flatten_errors(result))
        )

    # principal_path is [principal, actor1, actor2, ..., leaf]; nest outward-in.
    path = result.principal_path
    claim: dict[str, Any] = {"sub": path[-1]}
    for sub in reversed(path[:-1]):
        claim = {"sub": sub, "act": claim}
    return claim


def _flatten_errors(result: ChainVerification) -> list[str]:
    out = list(result.errors)
    for hop in result.hops:
        out.extend(f"hop {hop.hop_index}: {e}" for e in hop.errors)
    return out
