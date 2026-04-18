"""Agent credential scope minimization for least-privilege API keys.

Provides two layers of scoping:

1. **Logical scopes** (:class:`CredentialScope`, :class:`ScopedCredential`,
   :class:`CredentialScopeManager`) — restrict each logical credential to
   an operation/model/token budget.  These are enforced at request time by
   :func:`validate_request_against_scope`.

2. **Environment credential policy** (:class:`AgentCredentialPolicy`) —
   restrict which OS-level API-key env vars an agent subprocess is allowed
   to inherit.  This closes the audit-051 gap where every agent received
   the full provider key set from ``build_filtered_env``'s ``extra_keys``.

The policy is fail-closed: agents not explicitly listed receive **no**
credentials, and a request for an env var not declared in ``known_keys``
raises :class:`UnknownCredentialKeyError`.

Configuration surface (``.sdd/config/credential_scopes.yaml``)::

    enabled: true
    known_keys:
      - ANTHROPIC_API_KEY
      - OPENAI_API_KEY
      - OPENAI_ORG_ID
    agents:
      backend-001:
        - ANTHROPIC_API_KEY
      researcher-*:       # glob-style prefix match
        - OPENAI_API_KEY
    roles:
      backend:
        - ANTHROPIC_API_KEY
      researcher:
        - OPENAI_API_KEY

Load the policy once at orchestrator startup and register it::

    from bernstein.core.credential_scoping import (
        load_policy_from_file, set_default_policy,
    )
    set_default_policy(load_policy_from_file(path))

Then adapters call :func:`scoped_credential_keys` (or the ``agent_id`` param
of :func:`bernstein.adapters.env_isolation.build_filtered_env`) to obtain
the **filtered** subset of env-var keys for that agent.
"""

from __future__ import annotations

import fnmatch
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CredentialScopingError(Exception):
    """Base class for all credential-scoping policy violations."""


class UnknownCredentialKeyError(CredentialScopingError):
    """Raised when an adapter requests an env-var name not declared in the policy.

    The policy must enumerate every credential key that any agent might be
    granted.  Requesting an undeclared key is a configuration bug, not a
    permission denial — so we raise rather than silently drop.
    """


class AgentNotScopedError(CredentialScopingError):
    """Raised when a policy is enforced but the agent has no scope entry.

    The policy is fail-closed: if scoping is enabled and the agent
    identifier does not match any rule, spawning is aborted rather than
    silently granting or silently denying all keys.
    """


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
        tokens is None or scope.max_tokens_per_request is None or tokens <= scope.max_tokens_per_request
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
            if c.agent_id == agent_id and c.key_id not in self._revoked and datetime.now(UTC) < c.expires_at
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
        expired_keys = [kid for kid, cred in self._credentials.items() if now >= cred.expires_at]
        for kid in expired_keys:
            del self._credentials[kid]
            self._revoked.discard(kid)
        return len(expired_keys)


# Module-level default manager for convenience functions
_default_manager = CredentialScopeManager()


# ---------------------------------------------------------------------------
# Environment-level per-agent credential policy (audit-051)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCredentialPolicy:
    """Per-agent allowlist of OS-level credential env vars.

    The policy is **fail-closed**: once ``enabled`` is true, agents not
    covered by any rule get zero credentials.  Rules can be keyed by
    exact agent id, a glob-style pattern (``"backend-*"``), or by role
    name in the parallel ``roles`` map.

    Every env-var key referenced by any rule must appear in
    ``known_keys``; this prevents typos ("ANTHORPIC_API_KEY") from
    silently widening scope.

    Attributes:
        enabled: When ``False``, the policy is a no-op: callers get
            whatever keys they request.  Used so existing unscoped
            deployments keep working until opt-in.
        known_keys: The full set of env-var names the orchestrator
            knows how to scope.  Acts as a spell-check on the config.
        agent_rules: Mapping of agent-id (or glob pattern such as
            ``"backend-*"``) to the allowed subset of ``known_keys``.
        role_rules: Mapping of role name to allowed subset.  Consulted
            as a fallback when no ``agent_rules`` entry matches.
    """

    enabled: bool = False
    known_keys: frozenset[str] = field(default_factory=frozenset)
    agent_rules: dict[str, frozenset[str]] = field(default_factory=dict)
    role_rules: dict[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate every rule references only declared keys.
        for scope_name, rules in (("agent", self.agent_rules), ("role", self.role_rules)):
            for ident, keys in rules.items():
                bad = set(keys) - set(self.known_keys)
                if bad:
                    msg = (
                        f"credential policy {scope_name}_rules[{ident!r}] references "
                        f"undeclared keys {sorted(bad)!r}; add them to known_keys"
                    )
                    raise UnknownCredentialKeyError(msg)

    # -- Lookup ---------------------------------------------------------------

    def allowed_for(self, agent_id: str, *, role: str | None = None) -> frozenset[str]:
        """Return the set of env-var names this agent is permitted to inherit.

        Matching order:
        1. Exact ``agent_rules`` entry.
        2. Glob-style ``agent_rules`` entry (first match wins, in sorted order
           for determinism).
        3. ``role_rules[role]`` if ``role`` is supplied.

        Args:
            agent_id: Agent identifier (e.g. ``"backend-001"``).
            role: Optional role hint for fallback matching.

        Returns:
            Frozen set of allowed env-var names.

        Raises:
            AgentNotScopedError: If the policy is enabled and no rule
                matches (fail-closed).
        """
        if not self.enabled:
            # Disabled policy = transparent: caller's requested keys pass.
            return self.known_keys

        # 1. Exact match
        exact = self.agent_rules.get(agent_id)
        if exact is not None:
            return exact

        # 2. Glob match (sorted for determinism so the first wildcard
        #    win is reproducible across runs).
        for pattern in sorted(self.agent_rules):
            if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(agent_id, pattern):
                return self.agent_rules[pattern]

        # 3. Role fallback
        if role is not None:
            role_match = self.role_rules.get(role)
            if role_match is not None:
                return role_match

        msg = (
            f"agent {agent_id!r} (role={role!r}) is not covered by the credential policy; "
            "add an agent_rules or role_rules entry, or set enabled: false"
        )
        raise AgentNotScopedError(msg)

    def filter_keys(
        self,
        agent_id: str,
        requested_keys: Any,
        *,
        role: str | None = None,
    ) -> tuple[str, ...]:
        """Return the intersection of ``requested_keys`` and the agent's allowlist.

        This is the hot-path hook for :func:`build_filtered_env`.  It
        rejects unknown keys up front (config bug) and returns only the
        keys the agent is allowed to inherit.

        Args:
            agent_id: Agent identifier.
            requested_keys: Iterable of env-var names the adapter wants.
            role: Optional role for fallback matching.

        Returns:
            Tuple of permitted env-var names in stable (sorted) order.

        Raises:
            UnknownCredentialKeyError: Any requested key is not in
                ``known_keys``.  Raised regardless of ``enabled`` so
                typos are caught even in no-op mode.
            AgentNotScopedError: Policy is enabled and no rule matches
                the agent.
        """
        requested = frozenset(requested_keys)
        unknown = requested - self.known_keys
        if unknown and self.known_keys:
            # Only validate against known_keys when it is populated — an
            # empty known_keys means the policy is effectively inert and
            # should not block adapters that pre-date it.
            msg = (
                f"adapter requested unknown credential key(s) {sorted(unknown)!r}; "
                "declare them in credential policy known_keys"
            )
            raise UnknownCredentialKeyError(msg)

        if not self.enabled:
            # Pass-through: policy is informational only.
            return tuple(sorted(requested))

        allowed = self.allowed_for(agent_id, role=role)
        return tuple(sorted(requested & allowed))


# Module-level default policy — overridden at orchestrator startup.
_default_policy: AgentCredentialPolicy = AgentCredentialPolicy()


def get_default_policy() -> AgentCredentialPolicy:
    """Return the process-wide default :class:`AgentCredentialPolicy`.

    When no policy has been installed, the returned policy is disabled
    and acts as a no-op.
    """
    return _default_policy


def set_default_policy(policy: AgentCredentialPolicy) -> None:
    """Install a process-wide credential policy.

    Adapters that call :func:`scoped_credential_keys` without an explicit
    ``policy`` argument will consult the policy set here.  Call once at
    orchestrator startup.
    """
    global _default_policy
    _default_policy = policy


def scoped_credential_keys(
    agent_id: str,
    requested_keys: Any,
    *,
    role: str | None = None,
    policy: AgentCredentialPolicy | None = None,
) -> tuple[str, ...]:
    """Convenience wrapper around :meth:`AgentCredentialPolicy.filter_keys`.

    Uses the default policy from :func:`get_default_policy` when
    ``policy`` is not supplied.
    """
    effective = policy if policy is not None else _default_policy
    return effective.filter_keys(agent_id, requested_keys, role=role)


def load_policy_from_file(path: str | Path) -> AgentCredentialPolicy:
    """Load an :class:`AgentCredentialPolicy` from a YAML or JSON file.

    Expected schema::

        enabled: bool
        known_keys: [str, ...]
        agents:
          <agent-id-or-glob>: [str, ...]
        roles:
          <role>: [str, ...]

    Missing sections default to empty.  The file format is auto-detected
    from its extension (``.yaml``/``.yml`` → YAML, otherwise JSON).

    Args:
        path: Path to the policy file.

    Returns:
        A fully-constructed policy.

    Raises:
        FileNotFoundError: If the file does not exist.
        UnknownCredentialKeyError: If any rule references an undeclared key.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")

    data: dict[str, Any]
    if p.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # local import — yaml is a heavy dep

        loaded = yaml.safe_load(text) or {}
    else:
        import json

        loaded = json.loads(text) if text.strip() else {}

    if not isinstance(loaded, dict):
        msg = f"credential policy file {path} must contain a mapping at the top level"
        raise CredentialScopingError(msg)
    data = loaded

    known = frozenset(data.get("known_keys", ()) or ())
    agents_raw = data.get("agents", {}) or {}
    roles_raw = data.get("roles", {}) or {}

    agent_rules = {k: frozenset(v or ()) for k, v in agents_raw.items()}
    role_rules = {k: frozenset(v or ()) for k, v in roles_raw.items()}

    return AgentCredentialPolicy(
        enabled=bool(data.get("enabled", False)),
        known_keys=known,
        agent_rules=agent_rules,
        role_rules=role_rules,
    )
