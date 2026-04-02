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
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.auth import create_jwt, verify_jwt
from bernstein.core.sanitize import sanitize_log
from bernstein.core.tenanting import normalize_tenant_id

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

# Default permission sets by role, scoped to what agents need (not user RBAC).
AGENT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "manager": frozenset(
        {
            "tasks:read",
            "tasks:write",
            "agents:read",
            "agents:spawn",
            "status:read",
            "files:read",
            "files:write",
        }
    ),
    "backend": frozenset(
        {
            "tasks:read",
            "tasks:claim",
            "files:read",
            "files:write",
            "tests:run",
            "status:read",
        }
    ),
    "frontend": frozenset(
        {
            "tasks:read",
            "tasks:claim",
            "files:read",
            "files:write",
            "tests:run",
            "status:read",
        }
    ),
    "qa": frozenset(
        {
            "tasks:read",
            "tasks:claim",
            "files:read",
            "tests:run",
            "status:read",
        }
    ),
    "security": frozenset(
        {
            "tasks:read",
            "tasks:claim",
            "files:read",
            "files:write",
            "tests:run",
            "status:read",
        }
    ),
    "devops": frozenset(
        {
            "tasks:read",
            "tasks:claim",
            "files:read",
            "files:write",
            "tests:run",
            "status:read",
            "config:read",
        }
    ),
}

# Fallback for roles not listed above.
_DEFAULT_PERMISSIONS: frozenset[str] = frozenset(
    {
        "tasks:read",
        "tasks:claim",
        "files:read",
        "files:write",
        "status:read",
    }
)


def permissions_for_role(role: str) -> frozenset[str]:
    """Return the default permission set for an agent role."""
    return AGENT_ROLE_PERMISSIONS.get(role, _DEFAULT_PERMISSIONS)


# ---------------------------------------------------------------------------
# Agent credential (authentication token)
# ---------------------------------------------------------------------------


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
    token_type: Literal["opaque", "jwt"] = "opaque"
    algorithm: str = "HS256"
    jti: str = ""
    tenant_id: str = "default"

    @property
    def is_valid(self) -> bool:
        if self.revoked:
            return False
        return not (self.expires_at > 0 and time.time() > self.expires_at)

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
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentCredential:
        return cls(
            token_hash=str(d["token_hash"]),
            created_at=float(d.get("created_at", 0)),
            expires_at=float(d.get("expires_at", 0)),
            revoked=bool(d.get("revoked", False)),
            token_type=str(d.get("token_type", "opaque")),
            algorithm=str(d.get("algorithm", "HS256")),
            jti=str(d.get("jti", "")),
            tenant_id=normalize_tenant_id(str(d.get("tenant_id", "default") or "default")),
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

    @property
    def is_active(self) -> bool:
        return self.status == AgentIdentityStatus.ACTIVE

    def has_permission(self, permission: str) -> bool:
        """Check if this identity grants a specific permission."""
        return self.is_active and permission in self.permissions

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
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentIdentity:
        cred_data = d.get("credential")
        return cls(
            id=str(d["id"]),
            role=str(d["role"]),
            session_id=str(d["session_id"]),
            permissions=frozenset(d.get("permissions", [])),
            status=AgentIdentityStatus(d.get("status", "active")),
            created_at=float(d.get("created_at", 0)),
            last_authenticated_at=float(d.get("last_authenticated_at", 0)),
            revoked_at=float(d.get("revoked_at", 0)),
            revocation_reason=str(d.get("revocation_reason", "")),
            credential=AgentCredential.from_dict(cred_data) if cred_data else None,
            parent_identity_id=d.get("parent_identity_id"),
            metadata=dict(d.get("metadata", {})),
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
    """Return the agent-identity JWT secret, preferring the shared auth env var."""

    env_secret = os.environ.get("BERNSTEIN_AUTH_JWT_SECRET", "").strip()
    if env_secret:
        return env_secret

    secret_path = base_dir / "agent_identity_jwt_secret"
    if secret_path.exists():
        secret = secret_path.read_text(encoding="utf-8").strip()
        if secret:
            return secret

    secret = secrets.token_urlsafe(32)
    secret_path.write_text(secret, encoding="utf-8")
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

    def _load(self, identity_id: str) -> AgentIdentity | None:
        path = self._identity_path(identity_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return AgentIdentity.from_dict(data)

    def _rebuild_token_index(self) -> None:
        """Scan persisted identities and populate the token→identity lookup."""
        self._token_index.clear()
        if not self._identities_dir.exists():
            return
        for path in self._identities_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cred = data.get("credential")
                if cred and not cred.get("revoked", False):
                    self._token_index[cred["token_hash"]] = data["id"]
            except (json.JSONDecodeError, KeyError):
                logger.warning("Skipping corrupt identity file: %s", path)

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
    ) -> tuple[AgentIdentity, str]:
        """Create a new agent identity with a JWT bearer token.

        Returns the identity and the raw bearer token (shown only once).
        """
        identity_id = session_id  # 1:1 mapping with agent session
        permissions = permissions_for_role(role)
        if extra_permissions:
            permissions = permissions | extra_permissions

        now = time.time()
        expiry_s = int(token_expiry_s if token_expiry_s > 0 else 86400)
        tenant_id = normalize_tenant_id(str((metadata or {}).get("tenant_id", "default")))
        raw_token = create_jwt(
            claims={
                "sub": identity_id,
                "sid": session_id,
                "role": role,
                "scopes": sorted(permissions),
                "tenant_id": tenant_id,
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
                },
            )
        )

        logger.info("Created agent identity %s (role=%s)", identity_id, role)
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
        if identity.credential.token_type != "jwt":
            return None
        if identity.credential.token_hash != _hash_token(token):
            return None
        if identity.credential.jti and str(claims.get("jti", "")) != identity.credential.jti:
            return None
        if str(claims.get("sid", "")) != identity.session_id:
            return None
        if str(claims.get("role", "")) != identity.role:
            return None
        if normalize_tenant_id(str(claims.get("tenant_id", "default"))) != identity.credential.tenant_id:
            return None
        claim_scopes = claims.get("scopes", [])
        if not isinstance(claim_scopes, list) or set(map(str, claim_scopes)) != set(identity.permissions):
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

        if not identity.credential.is_valid:
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
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                identity = AgentIdentity.from_dict(data)
                if status is not None and identity.status != status:
                    continue
                if role is not None and identity.role != role:
                    continue
                results.append(identity)
            except (json.JSONDecodeError, KeyError):
                logger.warning("Skipping corrupt identity file: %s", path)
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
