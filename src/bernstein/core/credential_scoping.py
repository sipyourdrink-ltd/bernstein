"""Agent credential scope minimization for least-privilege API keys.

Provides scoped API credentials that restrict each agent to only the
operations, models, and token budgets it needs.  Instead of sharing a
single API key with full access, each agent receives a credential
whose scope is enforced locally — either via a proxy layer or via
pre-flight validation before dispatching requests to upstream
providers.

Usage::

    from bernstein.core.credential_scoping import (
        CredentialScope,
        ScopedCredential,
        CredentialScopeManager,
        create_scoped_credential,
        validate_request_against_scope,
        get_scope_for_role,
    )

    scope = get_scope_for_role("backend")
    cred = create_scoped_credential("agent-42", scope)
    is_valid = validate_request_against_scope(
        {"operation": "code_gen", "model": "gpt-4", "tokens": 500},
        cred.scope,
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialScope:
    """Scope definition for an agent credential.

    Attributes:
        allowed_operations: Operations this credential permits
            (e.g. ``"code_gen"``, ``"web_search"``, ``"file_read"``).
        allowed_models: If set, restrict which LLM models may be used.
        max_tokens_per_request: Per-request token budget cap.
        rate_limit_rpm: Maximum requests per minute.
    """

    allowed_operations: tuple[str, ...]
    allowed_models: tuple[str, ...] | None = None
    max_tokens_per_request: int | None = None
    rate_limit_rpm: int | None = None


@dataclass(frozen=True)
class ScopedCredential:
    """A scoped API credential for an agent.

    Attributes:
        key_id: Unique identifier for this credential.
        agent_id: The agent this credential is issued to.
        scope: The attached :class:`CredentialScope`.
        created_at: When the credential was created.
        expires_at: When the credential expires.
    """

    key_id: str
    agent_id: str
    scope: CredentialScope
    created_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# Pre-defined role scopes
# ---------------------------------------------------------------------------

_ROLE_SCOPES: dict[str, CredentialScope] = {
    "backend": CredentialScope(
        allowed_operations=("code_gen", "file_read", "file_write"),
        allowed_models=("gpt-4", "claude-sonnet-4-20250514"),
        max_tokens_per_request=8192,
        rate_limit_rpm=60,
    ),
    "frontend": CredentialScope(
        allowed_operations=("code_gen", "file_read", "file_write"),
        allowed_models=("gpt-4",),
        max_tokens_per_request=4096,
        rate_limit_rpm=30,
    ),
    "researcher": CredentialScope(
        allowed_operations=("web_search", "file_read"),
        allowed_models=("gpt-4",),
        max_tokens_per_request=2048,
        rate_limit_rpm=20,
    ),
    "analyst": CredentialScope(
        allowed_operations=("file_read", "code_gen"),
        allowed_models=("gpt-4",),
        max_tokens_per_request=4096,
        rate_limit_rpm=30,
    ),
    "admin": CredentialScope(
        allowed_operations=(
            "code_gen",
            "web_search",
            "file_read",
            "file_write",
            "system_admin",
        ),
        max_tokens_per_request=16384,
        rate_limit_rpm=120,
    ),
}

_DEFAULT_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def create_scoped_credential(
    agent_id: str,
    scope: CredentialScope,
    *,
    ttl_hours: int = _DEFAULT_TTL_HOURS,
) -> ScopedCredential:
    """Create a scoped API credential for an agent.

    Generates a unique ``key_id`` and sets the expiration to
    ``ttl_hours`` from now.

    Args:
        agent_id: Identifier of the agent receiving the credential.
        scope: The :class:`CredentialScope` to attach.
        ttl_hours: Time-to-live in hours before the credential expires.

    Returns:
        A new :class:`ScopedCredential`.
    """
    now = datetime.now(UTC)
    return ScopedCredential(
        key_id=f"sk-{uuid.uuid4().hex[:16]}",
        agent_id=agent_id,
        scope=scope,
        created_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )


def validate_request_against_scope(
    request: dict[str, Any],
    scope: CredentialScope,
) -> bool:
    """Validate that a request falls within the given credential scope.

    The ``request`` dict is expected to contain some subset of:

    - ``operation`` (``str``): The operation being performed.
    - ``model`` (``str``): The model being used.
    - ``tokens`` (``int``): Token count for this request.

    Args:
        request: The incoming request to validate.
        scope: The :class:`CredentialScope` to check against.

    Returns:
        ``True`` if the request is within scope, ``False`` otherwise.
    """
    # Check operation
    operation = request.get("operation")
    if operation is not None and operation not in scope.allowed_operations:
        return False

    # Check model
    model = request.get("model")
    if model is not None and scope.allowed_models is not None and model not in scope.allowed_models:
        return False

    # Check token budget
    tokens = request.get("tokens")
    tokens_within_budget = (
        tokens is None
        or scope.max_tokens_per_request is None
        or tokens <= scope.max_tokens_per_request
    )

    return tokens_within_budget


def revoke_credential(key_id: str) -> None:
    """Revoke a scoped credential.

    Delegates to the :class:`CredentialScopeManager` singleton.
    This is a convenience wrapper; for batch revocation use the manager
    directly.

    Args:
        key_id: The ``key_id`` of the credential to revoke.
    """
    _default_manager.revoke(key_id)


def get_scope_for_role(role: str) -> CredentialScope:
    """Return the default :class:`CredentialScope` for a role.

    If the role is unknown, returns a minimal read-only scope.

    Args:
        role: Role name (e.g. ``"backend"``, ``"researcher"``).

    Returns:
        The matching :class:`CredentialScope`.
    """
    return _ROLE_SCOPES.get(
        role,
        CredentialScope(
            allowed_operations=("file_read",),
            max_tokens_per_request=1024,
            rate_limit_rpm=10,
        ),
    )


# ---------------------------------------------------------------------------
# Credential manager
# ---------------------------------------------------------------------------


class CredentialScopeManager:
    """Manages scoped credentials lifecycle: creation, lookup, revocation.

    Usage::

        mgr = CredentialScopeManager()
        cred = mgr.create("agent-1", get_scope_for_role("backend"))
        assert mgr.is_valid(cred.key_id)
        mgr.revoke(cred.key_id)
        assert not mgr.is_valid(cred.key_id)
    """

    def __init__(self) -> None:
        self._credentials: dict[str, ScopedCredential] = {}
        self._revoked: set[str] = set()

    # -- Create ---------------------------------------------------------------

    def create(
        self,
        agent_id: str,
        scope: CredentialScope,
        *,
        ttl_hours: int = _DEFAULT_TTL_HOURS,
    ) -> ScopedCredential:
        """Create and store a scoped credential.

        Args:
            agent_id: Identifier of the agent.
            scope: The scope to attach.
            ttl_hours: Credential time-to-live in hours.

        Returns:
            The new :class:`ScopedCredential`.
        """
        cred = create_scoped_credential(agent_id, scope, ttl_hours=ttl_hours)
        self._credentials[cred.key_id] = cred
        return cred

    # -- Lookup ---------------------------------------------------------------

    def get(self, key_id: str) -> ScopedCredential | None:
        """Look up a credential by its ``key_id``.

        Args:
            key_id: The credential key identifier.

        Returns:
            The :class:`ScopedCredential`, or ``None`` if not found.
        """
        return self._credentials.get(key_id)

    def list_for_agent(self, agent_id: str) -> list[ScopedCredential]:
        """List all active credentials for an agent.

        Args:
            agent_id: The agent identifier.

        Returns:
            A list of matching :class:`ScopedCredential` instances.
        """
        return [
            c
            for c in self._credentials.values()
            if c.agent_id == agent_id
            and c.key_id not in self._revoked
            and datetime.now(UTC) < c.expires_at
        ]

    # -- Validation -----------------------------------------------------------

    def is_valid(self, key_id: str) -> bool:
        """Check whether a credential is still valid.

        A credential is valid if it exists, has not been revoked, and
        has not expired.

        Args:
            key_id: The credential key identifier.

        Returns:
            ``True`` if the credential is currently valid.
        """
        cred = self._credentials.get(key_id)
        if cred is None:
            return False
        if key_id in self._revoked:
            return False
        return datetime.now(UTC) < cred.expires_at

    def validate_request(self, key_id: str, request: dict[str, Any]) -> bool:
        """Validate a request against a credential's scope.

        Combines credential validity and scope checks in one call.

        Args:
            key_id: The credential key identifier.
            request: The incoming request dictionary.

        Returns:
            ``True`` if the credential is valid **and** the request is
            within scope.
        """
        if not self.is_valid(key_id):
            return False
        cred = self._credentials[key_id]
        return validate_request_against_scope(request, cred.scope)

    # -- Revocation -----------------------------------------------------------

    def revoke(self, key_id: str) -> None:
        """Revoke a credential.

        The credential remains in storage but is marked as revoked.

        Args:
            key_id: The credential key identifier.
        """
        self._revoked.add(key_id)

    def revoke_all_for_agent(self, agent_id: str) -> int:
        """Revoke all credentials for a given agent.

        Args:
            agent_id: The agent identifier.

        Returns:
            The number of credentials revoked.
        """
        count = 0
        for cred in self._credentials.values():
            if cred.agent_id == agent_id:
                self._revoked.add(cred.key_id)
                count += 1
        return count

    # -- Cleanup --------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Remove expired credentials from storage.

        Returns:
            The number of credentials removed.
        """
        now = datetime.now(UTC)
        expired_keys = [
            kid
            for kid, cred in self._credentials.items()
            if now >= cred.expires_at
        ]
        for kid in expired_keys:
            del self._credentials[kid]
            self._revoked.discard(kid)
        return len(expired_keys)


# Module-level default manager for convenience functions
_default_manager = CredentialScopeManager()
