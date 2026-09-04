"""Agent Identity Lifecycle Management.

First-class identities for agents: create, authenticate, authorize, audit, revoke.
Each agent session gets a unique identity with scoped permissions and a full
audit trail, following NIST AI Agent Standards for autonomous agent identities.

Identities are stored as JSON files in ``.sdd/auth/agent_identities/``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args

from bernstein.core.path_scope import (
    ScopePatternError,
    paths_outside_scope,
    validate_repo_relative_pattern,
)
from bernstein.core.security.auth import create_jwt, verify_jwt
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.security.tenanting import (
    DEFAULT_TENANT_ID,
    InvalidTenantIdError,
    normalize_tenant_id,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identity status
# ---------------------------------------------------------------------------


class AgentIdentityStatus(StrEnum):
    """Lifecycle status of an agent identity."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


# ---------------------------------------------------------------------------
# Scoped permissions for agents
# ---------------------------------------------------------------------------

# Permission string constants.
PERM_TASKS_READ: str = "tasks:read"
PERM_TASKS_WRITE: str = "tasks:write"
PERM_TASKS_CLAIM: str = "tasks:claim"
PERM_AGENTS_READ: str = "agents:read"
PERM_AGENTS_SPAWN: str = "agents:spawn"
PERM_STATUS_READ: str = "status:read"
PERM_FILES_READ: str = "files:read"
PERM_FILES_WRITE: str = "files:write"
PERM_TESTS_RUN: str = "tests:run"
PERM_CONFIG_READ: str = "config:read"

# Default permission sets by role, scoped to what agents need (not user RBAC).
AGENT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "manager": frozenset(
        {
            PERM_TASKS_READ,
            PERM_TASKS_WRITE,
            PERM_AGENTS_READ,
            PERM_AGENTS_SPAWN,
            PERM_STATUS_READ,
            PERM_FILES_READ,
            PERM_FILES_WRITE,
        }
    ),
    "backend": frozenset(
        {
            PERM_TASKS_READ,
            PERM_TASKS_CLAIM,
            PERM_FILES_READ,
            PERM_FILES_WRITE,
            PERM_TESTS_RUN,
            PERM_STATUS_READ,
        }
    ),
    "frontend": frozenset(
        {
            PERM_TASKS_READ,
            PERM_TASKS_CLAIM,
            PERM_FILES_READ,
            PERM_FILES_WRITE,
            PERM_TESTS_RUN,
            PERM_STATUS_READ,
        }
    ),
    "qa": frozenset(
        {
            PERM_TASKS_READ,
            PERM_TASKS_CLAIM,
            PERM_FILES_READ,
            PERM_TESTS_RUN,
            PERM_STATUS_READ,
        }
    ),
    "security": frozenset(
        {
            PERM_TASKS_READ,
            PERM_TASKS_CLAIM,
            PERM_FILES_READ,
            PERM_FILES_WRITE,
            PERM_TESTS_RUN,
            PERM_STATUS_READ,
        }
    ),
    "devops": frozenset(
        {
            PERM_TASKS_READ,
            PERM_TASKS_CLAIM,
            PERM_FILES_READ,
            PERM_FILES_WRITE,
            PERM_TESTS_RUN,
            PERM_STATUS_READ,
            PERM_CONFIG_READ,
        }
    ),
}

# Fallback for roles not listed above.
_DEFAULT_PERMISSIONS: frozenset[str] = frozenset(
    {
        PERM_TASKS_READ,
        PERM_TASKS_CLAIM,
        PERM_FILES_READ,
        PERM_FILES_WRITE,
        PERM_STATUS_READ,
    }
)


def permissions_for_role(role: str) -> frozenset[str]:
    """Return the default permission set for an agent role."""
    return AGENT_ROLE_PERMISSIONS.get(role, _DEFAULT_PERMISSIONS)


# ---------------------------------------------------------------------------
# Agent credential (authentication token)
# ---------------------------------------------------------------------------


# Sentinel for "the record has no ``tenant_id`` key at all", which is not the
# same thing as a key whose stored value happens to be ``null``.  Only the
# former is a pre-field record; see :func:`_credential_tenant_id`.
_TENANT_KEY_ABSENT: Final[object] = object()

#: The kinds a credential may declare.  ``opaque`` is verified against the
#: stored token hash alone; ``jwt`` additionally has its claims checked
#: against the identity, so which one a record says decides which validation
#: it gets (see :func:`_credential_token_type`).
TokenType = Literal["opaque", "jwt"]

#: The runtime spelling of :data:`TokenType`, derived from it rather than
#: restated.  A hand-written copy is one edit away from admitting less than
#: the type permits: adding a kind means touching the annotation, which is
#: where the type lives, and a stale allowlist then refuses a kind that
#: type-checks clean.  Deriving it makes that unreachable rather than
#: discouraged, which is what the comment here used to claim (#4015).
_CREDENTIAL_TOKEN_TYPES: Final[tuple[TokenType, ...]] = get_args(TokenType)


def _pattern_covered_by(child: str, parent_patterns: tuple[str, ...]) -> bool:
    """Return True when the parent's declared scope already admits ``child``.

    Decided by :func:`~bernstein.core.path_scope.paths_outside_scope`, the same
    matcher the merge gate reads ``allowed_files`` with, so the scope that mints
    a credential and the scope that admits its diff cannot disagree.  A
    string-prefix test would: ``src`` covers the path ``src`` and nothing under
    it, so treating it as a prefix would let a parent scoped to ``src`` mint a
    child scoped to ``src/secret.py`` -- a file the parent's own scope never
    admitted.  ``src/**`` is how a tree is admitted.

    A child that is itself a glob is covered only when the parent declared that
    same glob.  Whether one glob is contained in another is not a question this
    check guesses at, and refusing is the direction that cannot widen a scope.

    Deliberately not the prefix-coverage helper in
    :mod:`bernstein.core.security.capability_tokens`: that one answers this
    question for capability-token path *prefixes*, where ``src`` does cover
    ``src/secret.py``.  ``allowed_files`` is a glob field and the surface that
    enforces it is the merge gate, so it has to be read the way the merge gate
    reads it.
    """
    if child in parent_patterns:
        return True
    if any(wildcard in child for wildcard in "*?"):
        return False
    return not paths_outside_scope((child,), parent_patterns)


def _all_patterns_covered_by(child_patterns: set[str], parent_patterns: set[str]) -> bool:
    """Return True when every child pattern falls inside the parent's scope."""
    parents = tuple(sorted(parent_patterns))
    return all(_pattern_covered_by(child, parents) for child in child_patterns)


def _string_list(raw: Any, field: str) -> list[str]:
    """Return a persisted list-of-strings field, refusing any other shape.

    ``list()`` and ``frozenset()`` accept any iterable, which makes them the
    wrong tool for reading a stored authorization decision.  A stored string
    becomes a collection of its characters; a stored mapping becomes a
    collection of its *keys*, so ``{"admin:manage": 1}`` deserialises into a
    real held permission.  And an empty mapping becomes an empty list, which
    for ``task_ids`` and ``allowed_files`` is not "no data" but "no
    restriction" - collapsing a scoped credential into an unscoped one.

    These three fields are what an agent is allowed to do, so a value that is
    not a list of strings is refused rather than reinterpreted into one.  The
    ``ValueError`` is already handled by :meth:`AgentIdentityStore._read_identity`,
    so the record is skipped like any other unreadable one.

    Args:
        raw: The stored value.  An absent key is passed as an empty tuple by
            the caller; ``None`` is a stored null and is refused.
        field: Field name, for the error message.

    Raises:
        ValueError: The value is not a list, or holds a non-string entry.
    """
    if not isinstance(raw, list | tuple):
        raise ValueError(f"{field} must be a list, got {type(raw).__name__}")
    entries = cast("list[Any]", list(raw))
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError(f"{field} entries must be strings, got {type(entry).__name__}")
    return [str(entry) for entry in entries]


def _claim_string_list(raw: object) -> list[str] | None:
    """Return a signed-token claim as a list of strings, or ``None`` if it is not one.

    The same reasoning as :func:`_string_list`, applied to the claim side of
    the comparison rather than the stored side.  ``map(str)`` would coerce a
    claim before comparing it, so a claim carrying ``1`` would compare equal
    to a stored ``"1"`` and a claim carrying ``True`` to a stored ``"True"``.
    The point of the comparison is that the token's scope is the credential's
    scope, and a coerced match does not establish that.

    Returns ``None`` rather than raising: the caller turns any non-match into
    a failed authentication, and a malformed claim is a failed authentication
    like any other.
    """
    if not isinstance(raw, list | tuple):
        return None
    entries = cast("list[Any]", list(raw))
    if any(not isinstance(entry, str) for entry in entries):
        return None
    return [str(entry) for entry in entries]


def _require_matching_scope(field: str, on_identity: list[str], on_credential: list[str]) -> None:
    """Refuse a persisted identity whose scope disagrees with its credential's.

    Both copies of ``task_ids`` and ``allowed_files`` are read back as
    authorization state, but by different consumers: the request middleware
    reads the identity's copy, while the JWT claim check reads the
    credential's.  A record where the two disagree therefore has two
    different answers to "what may this agent act on", and the widest one
    wins wherever it happens to be read - an identity holding an empty list
    beside a scoped credential is treated as unrestricted by the middleware,
    and an opaque token never reaches the claim check that would have
    disagreed.

    Nothing writes such a record: ``create_identity`` puts the same list in
    both places.  A record carrying two answers was therefore hand-edited or
    written by something else, so it is refused through the same
    ``ValueError`` path as any other unreadable record rather than
    authenticated under whichever scope is read first.

    Raises:
        ValueError: The two copies of the field differ.
    """
    if sorted(on_identity) != sorted(on_credential):
        raise ValueError(
            f"{field} on the identity does not match the credential: {sorted(on_identity)} vs {sorted(on_credential)}"
        )


def _credential_tenant_id(raw: Any) -> str:
    """Return the tenant a persisted credential is scoped to.

    The value is read back as the authenticated scope for every request this
    credential authenticates, so the deserialisation boundary is where it has
    to be established as a real tenant id.  ``str()`` coercion would accept
    whatever shape happened to be stored and hand the result on as a usable
    scope, so a stored value that is not a non-blank string is refused
    instead.

    Leniency is keyed on the *key* being absent, not on the value being
    empty: a record written before the field existed carries no ``tenant_id``
    at all and belongs to :data:`DEFAULT_TENANT_ID`.  A record that carries
    the key with ``null`` in it is a different thing - something wrote a
    tenant and wrote a non-tenant - and it is refused like any other value
    that is not a real tenant id, rather than being quietly authenticated
    under the default tenant.

    Args:
        raw: The stored value, or :data:`_TENANT_KEY_ABSENT` when the record
            has no ``tenant_id`` key.

    Raises:
        ValueError: The record carries a ``tenant_id`` that is not a
            non-blank string.
    """
    if raw is _TENANT_KEY_ABSENT:
        return DEFAULT_TENANT_ID
    if not isinstance(raw, str):
        raise ValueError(f"credential tenant_id must be a string, got {type(raw).__name__}")
    if not raw.strip():
        raise ValueError("credential tenant_id must not be blank")
    return normalize_tenant_id(raw)


def _credential_token_type(raw: Any) -> TokenType:
    """Return the token kind a persisted credential declares.

    ``token_type`` selects which validation a token gets: ``_validate_jwt_claims``
    refuses anything whose credential does not say ``"jwt"``, so authentication
    falls through to the opaque hash comparison for every other value.  A
    ``str()`` coercion of the stored value therefore does not merely widen a
    type - it routes an authentication decision on a value nothing has
    established, and the routing is by whichever comparison runs first rather
    than by a recognised kind.

    An unknown kind is refused here instead, in the same style as
    :func:`_credential_tenant_id` beside it: the boundary where a record on
    disk becomes an object the rest of the code trusts is where a value that
    is not one of the two real kinds stops.

    Leniency is for age, not for content: a record written before the field
    existed carries no ``token_type`` at all and is an opaque credential,
    which is what the dataclass default has always said.  A record that
    carries the key with something else in it asserted a kind, and an
    unrecognised assertion is refused rather than defaulted - defaulting it
    would rewrite a security-relevant field on the way in and lose the fact
    that the store holds something nothing wrote.

    The ``isinstance`` check is what makes that refusal reachable for every
    stored shape rather than most of them.  A membership test hashes its left
    operand, and JSON persists two values that cannot be hashed: a list and
    an object.  Without the guard those two raise ``TypeError: unhashable
    type: 'list'`` out of the lookup itself - caught by
    :meth:`AgentIdentityStore._read_identity` like any other refusal, so the
    store stays readable, but naming neither the field nor the value, which
    is the whole point of the message below.  It is also the shape
    :func:`_credential_tenant_id` beside it already uses.

    Args:
        raw: The stored value, or ``"opaque"`` when the record has no
            ``token_type`` key.

    Raises:
        ValueError: The record carries a ``token_type`` outside
            :data:`TokenType`.
    """
    if isinstance(raw, str) and raw in _CREDENTIAL_TOKEN_TYPES:
        return cast("TokenType", raw)
    raise ValueError(f"credential token_type must be one of {sorted(_CREDENTIAL_TOKEN_TYPES)}, got {raw!r}")


@dataclass
class AgentCredential:
    """Bearer token for agent-to-server authentication.

    Each credential is tied to a single agent identity and carries a
    SHA-256 token hash (the raw token is returned only at creation time).
    """

    token_hash: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # 0 = no expiry (session-scoped)
    revoked: bool = False
    token_type: TokenType = "opaque"
    algorithm: str = "HS256"
    jti: str = ""
    tenant_id: str = "default"
    # Zero-trust: task scope - the task IDs this credential is authorised to act on.
    # An empty list means no task-scope restriction (legacy / manager tokens).
    task_ids: list[str] = field(default_factory=list)
    # Zero-trust: file scope - glob patterns for files this credential may write.
    # An empty list means no file-scope restriction.
    allowed_files: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        if self.revoked:
            return False
        return not (self.expires_at > 0 and time.time() > self.expires_at)

    def is_task_allowed(self, task_id: str) -> bool:
        """Return True if this credential is scoped to *task_id* (or has no scope)."""
        return not self.task_ids or task_id in self.task_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_hash": self.token_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "token_type": self.token_type,
            "algorithm": self.algorithm,
            "jti": self.jti,
            "tenant_id": self.tenant_id,
            "task_ids": self.task_ids.copy(),
            "allowed_files": self.allowed_files.copy(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentCredential:
        return cls(
            token_hash=str(d["token_hash"]),
            created_at=float(d.get("created_at", 0)),
            expires_at=float(d.get("expires_at", 0)),
            revoked=bool(d.get("revoked", False)),
            token_type=_credential_token_type(d.get("token_type", "opaque")),
            algorithm=str(d.get("algorithm", "HS256")),
            jti=str(d.get("jti", "")),
            tenant_id=_credential_tenant_id(d.get("tenant_id", _TENANT_KEY_ABSENT)),
            task_ids=_string_list(d.get("task_ids", ()), "credential task_ids"),
            allowed_files=_string_list(d.get("allowed_files", ()), "credential allowed_files"),
        )


# ---------------------------------------------------------------------------
# Agent identity
# ---------------------------------------------------------------------------


@dataclass
class AgentIdentity:
    """First-class identity for an agent session.

    Each agent gets a unique identity with scoped permissions. Identities
    persist across restarts in ``.sdd/auth/agent_identities/`` as JSON.

    Attributes:
        id: Unique identity ID (matches the agent session ID).
        role: Agent role (backend, qa, security, etc.).
        session_id: The spawned agent session this identity belongs to.
        permissions: Set of granted permission strings.
        status: Current lifecycle status.
        created_at: Unix timestamp of identity creation.
        last_authenticated_at: Last successful authentication timestamp.
        revoked_at: Timestamp when identity was revoked (0 if active).
        revocation_reason: Why the identity was revoked.
        credential: Bearer token credential for authentication.
        parent_identity_id: ID of the spawning agent's identity (delegation).
        metadata: Arbitrary metadata (cell_id, provider, model, etc.).
    """

    id: str
    role: str
    session_id: str
    permissions: frozenset[str] = field(default_factory=frozenset)
    status: AgentIdentityStatus = AgentIdentityStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    last_authenticated_at: float = 0.0
    revoked_at: float = 0.0
    revocation_reason: str = ""
    credential: AgentCredential | None = None
    parent_identity_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Zero-trust task scope - tasks this identity is allowed to report on.
    # Empty means unrestricted (manager / orchestrator tokens).
    task_ids: list[str] = field(default_factory=list)
    # Zero-trust file scope - glob patterns for files this identity may write.
    allowed_files: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status == AgentIdentityStatus.ACTIVE

    def has_permission(self, permission: str) -> bool:
        """Check if this identity grants a specific permission."""
        return self.is_active and permission in self.permissions

    def is_task_allowed(self, task_id: str) -> bool:
        """Return True if this identity is scoped to *task_id* (or has no scope)."""
        return not self.task_ids or task_id in self.task_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "session_id": self.session_id,
            "permissions": sorted(self.permissions),
            "status": self.status.value,
            "created_at": self.created_at,
            "last_authenticated_at": self.last_authenticated_at,
            "revoked_at": self.revoked_at,
            "revocation_reason": self.revocation_reason,
            "credential": self.credential.to_dict() if self.credential else None,
            "parent_identity_id": self.parent_identity_id,
            "metadata": self.metadata,
            "task_ids": self.task_ids.copy(),
            "allowed_files": self.allowed_files.copy(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentIdentity:
        cred_data = d.get("credential")
        credential = AgentCredential.from_dict(cred_data) if cred_data else None
        task_ids = _string_list(d.get("task_ids", ()), "task_ids")
        allowed_files = _string_list(d.get("allowed_files", ()), "allowed_files")
        if credential is not None:
            _require_matching_scope("task_ids", task_ids, credential.task_ids)
            _require_matching_scope("allowed_files", allowed_files, credential.allowed_files)
        return cls(
            id=str(d["id"]),
            role=str(d["role"]),
            session_id=str(d["session_id"]),
            permissions=frozenset(_string_list(d.get("permissions", ()), "permissions")),
            status=AgentIdentityStatus(d.get("status", "active")),
            created_at=float(d.get("created_at", 0)),
            last_authenticated_at=float(d.get("last_authenticated_at", 0)),
            revoked_at=float(d.get("revoked_at", 0)),
            revocation_reason=str(d.get("revocation_reason", "")),
            credential=credential,
            parent_identity_id=d.get("parent_identity_id"),
            metadata=dict(d.get("metadata", {})),
            task_ids=task_ids,
            allowed_files=allowed_files,
        )


# ---------------------------------------------------------------------------
# Identity audit event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityAuditEvent:
    """Audit record for agent identity lifecycle actions."""

    timestamp: float
    identity_id: str
    action: str  # "created", "authenticated", "authorized", "denied", "revoked", "suspended"
    actor: str  # who/what triggered it
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "identity_id": self.identity_id,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Identity store (file-based persistence)
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    """SHA-256 hash of a bearer token."""
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def _load_or_create_jwt_secret(base_dir: Path) -> str:
    """Return the agent-identity JWT secret, preferring the shared auth env var.

    When a new secret must be generated and persisted to disk, the file is
    created with mode 0600 (owner read/write only) to prevent other users or
    processes on the same host from reading the key material.
    """
    env_secret = os.environ.get("BERNSTEIN_AUTH_JWT_SECRET", "").strip()
    if env_secret:
        return env_secret

    secret_path = base_dir / "agent_identity_jwt_secret"
    if secret_path.exists():
        secret = secret_path.read_text(encoding="utf-8").strip()
        if secret:
            return secret

    secret = secrets.token_urlsafe(32)

    fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode("utf-8"))
    finally:
        os.close(fd)

    return secret


class AgentIdentityStore:
    """File-based CRUD store for agent identities.

    Identities are stored as JSON files in ``<base_dir>/agent_identities/``.
    Audit events are appended to ``<base_dir>/agent_identity_audit.jsonl``.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._identities_dir = base_dir / "agent_identities"
        self._identities_dir.mkdir(parents=True, exist_ok=True)
        self._audit_path = base_dir / "agent_identity_audit.jsonl"
        self._jwt_secret = _load_or_create_jwt_secret(base_dir)
        # In-memory index keyed by token_hash → identity_id for fast auth.
        self._token_index: dict[str, str] = {}
        self._rebuild_token_index()

    # -- persistence --------------------------------------------------------

    def _identity_path(self, identity_id: str) -> Path:
        return self._identities_dir / f"{identity_id}.json"

    def _save(self, identity: AgentIdentity) -> None:
        path = self._identity_path(identity.id)
        path.write_text(json.dumps(identity.to_dict(), indent=2), encoding="utf-8")

    def _read_identity(self, path: Path) -> AgentIdentity | None:
        """Deserialise one persisted identity file, or None when unusable.

        Every reader in this store goes through here, so a record that cannot
        be turned into an identity is skipped identically everywhere: the
        startup token-index scan, ``list_identities``, and the ``_load``
        lookup behind authentication and the lifecycle mutators.  Before this
        was shared, each reader picked off raw JSON with its own exception
        list, so the same bad file could block startup on one path, escape as
        a 500 on another, and merely be omitted on a third.

        Everything a bad record can raise is caught: a top-level value that
        is not an object, a nested value that is not subscriptable, a missing
        key, an enum or tenant value that is not a legitimate one, and the
        filesystem errors of reading the file at all.  All of them mean the
        same thing to a caller - there is no identity here - so all of them
        produce ``None`` rather than an exception the caller cannot act on.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                msg = f"identity record must be a JSON object, got {type(data).__name__}"
                raise TypeError(msg)
            return AgentIdentity.from_dict(cast("dict[str, Any]", data))
        except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
            logger.warning("Skipping corrupt identity file: %s", path)
            return None

    def _load(self, identity_id: str) -> AgentIdentity | None:
        """Read one identity by id, or None when it is missing or unusable."""
        path = self._identity_path(identity_id)
        if not path.exists():
            return None
        return self._read_identity(path)

    def _rebuild_token_index(self) -> None:
        """Scan persisted identities and populate the token→identity lookup.

        Runs from ``__init__``, so anything that escapes here blocks server
        startup.  It indexes only records that fully deserialise, which is
        also the only useful set: a record ``_read_identity`` rejects cannot
        authenticate anyway, so indexing its token would map a live token to
        an identity that always resolves to ``None``.
        """
        self._token_index.clear()
        if not self._identities_dir.exists():
            return
        for path in self._identities_dir.glob("*.json"):
            identity = self._read_identity(path)
            if identity is None:
                continue
            cred = identity.credential
            if cred is not None and not cred.revoked:
                self._token_index[cred.token_hash] = identity.id

    def _append_audit(self, event: IdentityAuditEvent) -> None:
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

    # -- CRUD operations ----------------------------------------------------

    def create_identity(
        self,
        session_id: str,
        role: str,
        *,
        parent_identity_id: str | None = None,
        extra_permissions: frozenset[str] | None = None,
        metadata: dict[str, Any] | None = None,
        token_expiry_s: float = 0.0,
        task_ids: list[str] | None = None,
        allowed_files: list[str] | None = None,
    ) -> tuple[AgentIdentity, str]:
        """Create a new agent identity with a short-lived, task-scoped JWT.

        Each agent receives a JWT that is scoped to its assigned task IDs and
        (optionally) a list of file glob patterns its work may touch.  The task
        server enforces ``task_ids`` on every incoming request, so a compromised
        agent cannot modify tasks outside its own scope.  ``allowed_files`` is
        enforced at a different place and buys a different thing: the merge
        acceptance gate refuses to bring in a change that falls outside it
        (#3914), which contains an out-of-scope write rather than preventing
        it.  See ``docs/operations/security-and-identity.md``.

        Args:
            session_id: Unique agent session identifier.
            role: Agent role (backend, qa, security, etc.).
            parent_identity_id: ID of the spawning agent's identity.
            extra_permissions: Additional permissions beyond the role defaults.
            metadata: Arbitrary metadata (cell_id, provider, model, tenant_id).
            token_expiry_s: Seconds until the token expires.  Defaults to 4 h for
                task-scoped tokens or 24 h for unrestricted manager tokens.
            task_ids: Task IDs this identity is authorised to act on.  An empty
                list means no restriction (orchestrator / manager role).
            allowed_files: Repository-relative glob patterns this identity's
                work may touch.  An empty list means no restriction, which is
                what every identity minted before the gate existed carries.
                Each pattern is validated here, so a scope that could name a
                file outside the repository never becomes a signed one.

        Returns:
            Tuple of ``(AgentIdentity, raw_token)`` - the raw bearer token is
            returned exactly once and must be passed to the agent securely.

        Raises:
            ValueError: ``task_ids`` or ``allowed_files`` is not a list of
                strings, or an ``allowed_files`` pattern is not
                repository-relative.  Refused before the token is signed, so a
                bad scope cannot become a credential that fails to load.
        """
        identity_id = session_id  # 1:1 mapping with agent session
        permissions = permissions_for_role(role)
        if extra_permissions:
            permissions = permissions | extra_permissions

        # Validated here rather than at the read side alone: these two lists are
        # signed into the token and persisted beside it, so a caller passing a
        # non-string entry would mint a credential that its own reader refuses,
        # leaving an agent whose token authenticates as an unknown identity.
        # Only ``None`` means "not supplied".  ``or ()`` would have sent every
        # falsy value down the unrestricted path, so an empty mapping or an
        # empty string - refused as corrupt when read back - would instead have
        # signed a token with no task scope at all.
        scoped_task_ids = _string_list(() if task_ids is None else task_ids, "task_ids")
        scoped_files = _string_list(() if allowed_files is None else allowed_files, "allowed_files")

        # When a parent identity is named, the child's task_ids and allowed_files
        # must be a subset of the parent's.  An unrestricted parent (empty
        # task_ids) may mint anything; a restricted parent may only mint children
        # that narrow its scope.  This prevents a child from holding a scope its
        # parent never held, which was the original issue #5046.
        if parent_identity_id is not None:
            parent_identity = self._load(parent_identity_id)
            if parent_identity is None:
                msg = f"parent identity {parent_identity_id} not found"
                raise ValueError(msg)

            # task_ids is an allowlist: an empty parent list means unrestricted,
            # so the child may name anything; otherwise the child must be a
            # subset, and an empty child narrows to nothing.  This is set
            # containment, the same relation
            # ``bernstein.core.security.capability_tokens.allowlist_narrows``
            # states for two present sets; it is spelled out here rather than
            # imported because that module imports this one.
            if parent_identity.task_ids:
                child_ids = set(scoped_task_ids)
                parent_ids = set(parent_identity.task_ids)
                if not child_ids <= parent_ids:
                    raise ValueError(
                        f"child task_ids {sorted(child_ids)} are not a subset of parent task_ids {sorted(parent_ids)}"
                    )

            # allowed_files: every child pattern must fall inside the parent's
            # scope, read by the same matcher the merge gate uses.  An empty
            # parent scope means unrestricted, so the child may name anything.
            # An empty child scope under a restricted parent is the widening
            # direction -- empty means unrestricted -- so it is refused rather
            # than passing vacuously.
            if parent_identity.allowed_files:
                child_patterns = set(scoped_files)
                parent_patterns = set(parent_identity.allowed_files)
                if not child_patterns or not _all_patterns_covered_by(child_patterns, parent_patterns):
                    raise ValueError(
                        f"child allowed_files {sorted(child_patterns)} are not a subset of "
                        f"parent allowed_files {sorted(parent_patterns)}"
                    )
        # Shape is not enough for the file scope: the merge gate reads these as
        # repository-relative globs, so a pattern that names a drive or walks
        # out of the root is refused before it can be signed.  Validated only
        # here, at declaration -- records written before this existed stay
        # loadable, and an uninterpretable pattern simply admits nothing when
        # the gate matches against it.
        for index, pattern in enumerate(scoped_files):
            try:
                validate_repo_relative_pattern(pattern)
            except ScopePatternError as exc:
                msg = f"allowed_files[{index}]: {exc}"
                raise ValueError(msg) from exc

        now = time.time()
        # Use shorter expiry (4 h) for task-scoped tokens to limit blast radius.
        default_expiry = 14400 if scoped_task_ids else 86400
        expiry_s = int(token_expiry_s if token_expiry_s > 0 else default_expiry)
        tenant_id = normalize_tenant_id(str((metadata or {}).get("tenant_id", "default")))
        raw_token = create_jwt(
            claims={
                "sub": identity_id,
                "sid": session_id,
                "role": role,
                "scopes": sorted(permissions),
                "tenant_id": tenant_id,
                "task_ids": scoped_task_ids,
                "allowed_files": scoped_files,
            },
            secret=self._jwt_secret,
            expiry_seconds=expiry_s,
        )
        claims = verify_jwt(raw_token, self._jwt_secret)
        if claims is None:
            msg = "failed to verify freshly issued agent JWT"
            raise RuntimeError(msg)
        token_hash = _hash_token(raw_token)

        credential = AgentCredential(
            token_hash=token_hash,
            created_at=now,
            expires_at=float(claims.get("exp", now + expiry_s)),
            token_type="jwt",
            algorithm="HS256",
            jti=str(claims.get("jti", "")),
            tenant_id=tenant_id,
            task_ids=scoped_task_ids,
            allowed_files=scoped_files,
        )

        identity = AgentIdentity(
            id=identity_id,
            role=role,
            session_id=session_id,
            permissions=permissions,
            status=AgentIdentityStatus.ACTIVE,
            created_at=now,
            credential=credential,
            parent_identity_id=parent_identity_id,
            metadata=metadata or {},
            task_ids=scoped_task_ids,
            allowed_files=scoped_files,
        )

        self._save(identity)
        self._token_index[token_hash] = identity_id

        self._append_audit(
            IdentityAuditEvent(
                timestamp=now,
                identity_id=identity_id,
                action="created",
                actor="spawner",
                details={
                    "role": role,
                    "permissions": sorted(permissions),
                    "parent_identity_id": parent_identity_id,
                    "token_type": credential.token_type,
                    "task_ids": scoped_task_ids,
                    "has_file_scope": bool(scoped_files),
                },
            )
        )

        logger.info(
            "Created agent identity %s (role=%s, tasks=%s)",
            identity_id,
            role,
            scoped_task_ids or "unrestricted",
        )
        return identity, raw_token

    def authenticate(self, token: str) -> AgentIdentity | None:
        """Authenticate a bearer token and return the identity, or None."""
        jwt_identity = self._authenticate_jwt(token)
        if jwt_identity is not None:
            return jwt_identity

        token_hash = _hash_token(token)
        identity_id = self._token_index.get(token_hash)
        if identity_id is None:
            return None

        identity = self._load(identity_id)
        if identity is None:
            return None

        if not identity.is_active:
            self._append_audit(
                IdentityAuditEvent(
                    timestamp=time.time(),
                    identity_id=identity_id,
                    action="denied",
                    actor="auth",
                    details={"reason": f"identity status: {identity.status}"},
                )
            )
            return None

        if identity.credential and not identity.credential.is_valid:
            self._append_audit(
                IdentityAuditEvent(
                    timestamp=time.time(),
                    identity_id=identity_id,
                    action="denied",
                    actor="auth",
                    details={"reason": "credential expired or revoked"},
                )
            )
            return None

        # Update last-authenticated timestamp.
        identity.last_authenticated_at = time.time()
        self._save(identity)

        self._append_audit(
            IdentityAuditEvent(
                timestamp=time.time(),
                identity_id=identity_id,
                action="authenticated",
                actor="auth",
            )
        )
        return identity

    def _authenticate_jwt(self, token: str) -> AgentIdentity | None:
        """Authenticate a JWT token when the credential was issued in JWT mode."""

        claims = verify_jwt(token, self._jwt_secret)
        if not claims:
            return None

        identity_id = str(claims.get("sub", ""))
        if not identity_id:
            return None

        identity = self._load(identity_id)
        if identity is None or identity.credential is None:
            return None

        if not self._validate_jwt_claims(claims, identity, token):
            return None

        if not identity.is_active:
            self._audit_denial(identity_id, f"identity status: {identity.status}")
            return None

        if not identity.credential.is_valid:
            self._audit_denial(identity_id, "credential expired or revoked")
            return None

        identity.last_authenticated_at = time.time()
        self._save(identity)
        self._append_audit(
            IdentityAuditEvent(
                timestamp=time.time(),
                identity_id=identity_id,
                action="authenticated",
                actor="auth",
                details={"token_type": "jwt"},
            )
        )
        return identity

    def _validate_jwt_claims(self, claims: dict[str, object], identity: AgentIdentity, token: str) -> bool:
        """Validate JWT claims against stored identity and credential."""
        cred = identity.credential
        assert cred is not None  # caller guarantees this
        if cred.token_type != "jwt":
            return False
        if cred.token_hash != _hash_token(token):
            return False
        if cred.jti and str(claims.get("jti", "")) != cred.jti:
            return False
        if str(claims.get("sid", "")) != identity.session_id:
            return False
        if str(claims.get("role", "")) != identity.role:
            return False
        # A malformed tenant claim can never match the credential's own
        # (already valid) tenant, so it is a claim mismatch like any other -
        # deny rather than letting the refusal escape this bool-returning
        # validator and surface as a server error at the auth boundary.
        try:
            claim_tenant = normalize_tenant_id(str(claims.get("tenant_id", "default")))
        except InvalidTenantIdError:
            return False
        if claim_tenant != cred.tenant_id:
            return False
        claim_scopes = _claim_string_list(claims.get("scopes", []))
        if claim_scopes is None or set(claim_scopes) != set(identity.permissions):
            return False
        claim_task_ids = _claim_string_list(claims.get("task_ids", []))
        if claim_task_ids is None or sorted(claim_task_ids) != sorted(cred.task_ids):
            return False
        claim_files = _claim_string_list(claims.get("allowed_files", []))
        return claim_files is not None and sorted(claim_files) == sorted(cred.allowed_files)

    def _audit_denial(self, identity_id: str, reason: str) -> None:
        """Log a denied authentication attempt."""
        self._append_audit(
            IdentityAuditEvent(
                timestamp=time.time(),
                identity_id=identity_id,
                action="denied",
                actor="auth",
                details={"reason": reason},
            )
        )

    def authorize(self, identity_id: str, permission: str, *, actor: str = "authz") -> bool:
        """Check if an identity has a specific permission. Logs the result."""
        identity = self._load(identity_id)
        if identity is None:
            return False

        granted = identity.has_permission(permission)

        self._append_audit(
            IdentityAuditEvent(
                timestamp=time.time(),
                identity_id=identity_id,
                action="authorized" if granted else "denied",
                actor=actor,
                details={"permission": permission, "granted": granted},
            )
        )
        return granted

    def validate_task_access(self, identity_id: str, task_id: str) -> bool:
        """Return True if *identity_id* is permitted to act on *task_id*.

        An identity with no task scope (``task_ids == []``) is unrestricted and
        always passes.  An identity with an explicit task list only passes when
        *task_id* is in that list.

        This check is enforced by the task server middleware on every
        task-mutating request so that a compromised agent cannot affect tasks
        outside its scope.

        Args:
            identity_id: The agent identity to check.
            task_id: The task being acted on.

        Returns:
            True if access is permitted, False otherwise.
        """
        identity = self._load(identity_id)
        if identity is None or not identity.is_active:
            return False
        allowed = identity.is_task_allowed(task_id)
        if not allowed:
            self._append_audit(
                IdentityAuditEvent(
                    timestamp=time.time(),
                    identity_id=identity_id,
                    action="denied",
                    actor="task-scope",
                    details={
                        "task_id": task_id,
                        "reason": "task not in identity scope",
                        "allowed_tasks": identity.task_ids,
                    },
                )
            )
        return allowed

    def revoke(self, identity_id: str, *, reason: str = "", actor: str = "admin") -> bool:
        """Revoke an agent identity. Returns True if the identity was found."""
        identity = self._load(identity_id)
        if identity is None:
            return False

        now = time.time()
        identity.status = AgentIdentityStatus.REVOKED
        identity.revoked_at = now
        identity.revocation_reason = reason
        if identity.credential:
            identity.credential.revoked = True

        self._save(identity)

        # Remove from token index.
        if identity.credential:
            self._token_index.pop(identity.credential.token_hash, None)

        self._append_audit(
            IdentityAuditEvent(
                timestamp=now,
                identity_id=identity_id,
                action="revoked",
                actor=actor,
                details={"reason": reason},
            )
        )
        logger.info(
            "Revoked agent identity %s: %s",
            sanitize_log(identity_id),
            sanitize_log(reason),
        )
        return True

    def suspend(self, identity_id: str, *, reason: str = "", actor: str = "admin") -> bool:
        """Suspend an agent identity (reversible). Returns True if found."""
        identity = self._load(identity_id)
        if identity is None:
            return False

        identity.status = AgentIdentityStatus.SUSPENDED
        self._save(identity)

        self._append_audit(
            IdentityAuditEvent(
                timestamp=time.time(),
                identity_id=identity_id,
                action="suspended",
                actor=actor,
                details={"reason": reason},
            )
        )
        logger.info(
            "Suspended agent identity %s: %s",
            sanitize_log(identity_id),
            sanitize_log(reason),
        )
        return True

    def reactivate(self, identity_id: str, *, actor: str = "admin") -> bool:
        """Reactivate a suspended identity. Returns True if found and was suspended."""
        identity = self._load(identity_id)
        if identity is None:
            return False
        if identity.status != AgentIdentityStatus.SUSPENDED:
            return False

        identity.status = AgentIdentityStatus.ACTIVE
        self._save(identity)

        self._append_audit(
            IdentityAuditEvent(
                timestamp=time.time(),
                identity_id=identity_id,
                action="reactivated",
                actor=actor,
            )
        )
        logger.info("Reactivated agent identity %s", identity_id)
        return True

    def get(self, identity_id: str) -> AgentIdentity | None:
        """Load a single identity by ID."""
        return self._load(identity_id)

    def list_identities(
        self,
        *,
        status: AgentIdentityStatus | None = None,
        role: str | None = None,
    ) -> list[AgentIdentity]:
        """List all identities, optionally filtered by status and/or role."""
        results: list[AgentIdentity] = []
        for path in sorted(self._identities_dir.glob("*.json")):
            # Shared reader: a record that does not deserialise is skipped
            # rather than listed with a value derived from a bad record, and
            # a malformed file cannot escape this route as a 500 either.
            identity = self._read_identity(path)
            if identity is None:
                continue
            if status is not None and identity.status != status:
                continue
            if role is not None and identity.role != role:
                continue
            results.append(identity)
        return results

    def get_audit_trail(self, identity_id: str | None = None, *, limit: int = 100) -> list[IdentityAuditEvent]:
        """Read audit events, optionally filtered to a single identity."""
        events: list[IdentityAuditEvent] = []
        if not self._audit_path.exists():
            return events
        for line in self._audit_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if identity_id and data.get("identity_id") != identity_id:
                    continue
                events.append(
                    IdentityAuditEvent(
                        timestamp=float(data["timestamp"]),
                        identity_id=str(data["identity_id"]),
                        action=str(data["action"]),
                        actor=str(data["actor"]),
                        details=dict(data.get("details", {})),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        return events[-limit:]
