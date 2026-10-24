"""Per-role adapter deny-list policy.

The existing per-role tool deny-list (``claude_permission_profiles.py``)
operates at the *tool* granularity ("backend role can't run ``Bash``")
but cannot say "security role cannot spawn the cloud-LLM
``claude_routine`` adapter". This module adds the missing layer: a role
→ allow-list of adapters, enforced at the adapter-spawn site
(``bernstein.adapters.registry.get_adapter``).

Default semantics - back-compat:

* Empty allow-list for a role = **all adapters allowed**. Existing operators
  see no behaviour change after the policy module loads.
* A non-empty allow-list = strict - any adapter not on the list raises
  :exc:`RoleAdapterDenied` and emits a structured ``role.adapter.denied``
  audit event.

Mapping to standards:

* AIGF ``CTRL-SEGREGATION-OF-DUTIES``.
* SR 11-7 §V.4 segregation of duties.
* ISO 42001 cl. 7.5.3 control of documented information / role-based
  access.

The module is intentionally light: a single dataclass + a global accessor.
The ``get_policy`` / ``set_policy`` pair lets the CLI mutate the in-process
state without forcing every caller to thread the policy through their
arguments.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)

#: Audit event type emitted when a deny is enforced. Shipped as a constant so
#: log analysers can grep for it without recompiling.
ADAPTER_DENY_EVENT_TYPE: str = "role.adapter.denied"


class PolicyFileCorrupt(OSError):
    """Raised when a policy file exists but cannot be read or parsed.

    Distinct from the not-found case (which returns the permit-all default) so
    callers on the security-enforcement path can fail closed rather than
    silently lifting all restrictions.
    """


class RoleAdapterDenied(RuntimeError):
    """Raised when a role attempts to spawn an adapter outside its allow-list.

    Carries the role name and adapter id so call sites can format errors
    consistently.
    """

    def __init__(self, role: str, adapter: str, *, allowed: tuple[str, ...]) -> None:
        self.role = role
        self.adapter = adapter
        self.allowed = allowed
        msg = (
            f"role {role!r} is not allowed to spawn adapter {adapter!r}; allowed adapters: {sorted(allowed) or 'none'}"
        )
        super().__init__(msg)


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """Per-role adapter allow-list policy.

    Attributes:
        per_role_allowlists: ``{role: (adapter, ...)}``. Empty tuple for a
            role = all adapters allowed (back-compat). Roles missing from
            the map are also unrestricted.
    """

    per_role_allowlists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def is_allowed(self, role: str, adapter: str) -> bool:
        """Return True iff *role* may spawn *adapter* under this policy."""
        allowed = self.per_role_allowlists.get(role)
        if not allowed:
            return True
        return adapter in allowed

    def allowed_for(self, role: str) -> tuple[str, ...]:
        """Return the configured allow-list for *role* (empty = all allowed)."""
        return tuple(self.per_role_allowlists.get(role, ()))

    def to_dict(self) -> dict[str, list[str]]:
        """Serialise to a JSON-safe dict (sorted for determinism)."""
        return {role: sorted(allowed) for role, allowed in sorted(self.per_role_allowlists.items())}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RolePolicy:
        """Build a :class:`RolePolicy` from a JSON-shaped mapping.

        Args:
            data: ``{role: [adapter, ...]}``. Empty list / non-list values
                are normalised to an empty tuple (no-op for that role).

        Returns:
            A frozen :class:`RolePolicy`.
        """
        normalised: dict[str, tuple[str, ...]] = {}
        for role, allowed in data.items():
            if isinstance(allowed, (list, tuple, set)):
                normalised[role] = tuple(sorted({str(a) for a in allowed}))
            else:
                normalised[role] = ()
        return cls(per_role_allowlists=normalised)


# ---------------------------------------------------------------------------
# Global accessor
# ---------------------------------------------------------------------------


_state_lock = threading.Lock()
_active_policy: RolePolicy = RolePolicy()


def get_policy() -> RolePolicy:
    """Return the currently-active policy (default: empty / unrestricted)."""
    with _state_lock:
        return _active_policy


def set_policy(policy: RolePolicy) -> RolePolicy:
    """Install a new active policy. Returns the previous one."""
    global _active_policy
    with _state_lock:
        previous = _active_policy
        _active_policy = policy
    return previous


def reset_policy() -> None:
    """Reset to the default (empty / unrestricted) policy. Used by tests."""
    set_policy(RolePolicy())


# ---------------------------------------------------------------------------
# Persistence helpers (CLI uses these - keeps the CLI itself trivial)
# ---------------------------------------------------------------------------


#: Default location for the JSON policy file.
DEFAULT_POLICY_PATH: Path = Path(".sdd/security/role_adapter_policy.json")


def load_policy_file(path: Path = DEFAULT_POLICY_PATH) -> RolePolicy:
    """Load a policy from disk; return the empty (permit-all) policy when no file exists.

    Args:
        path: JSON file with the ``{role: [adapter, ...]}`` shape.

    Returns:
        A :class:`RolePolicy`. Returns the empty policy when the file does not
        exist — that means no restrictions were ever configured.

    Raises:
        PolicyFileCorrupt: When the file *exists* but cannot be read or parsed.
            "Missing" and "unreadable" are different security postures: missing
            means no policy was set; unreadable means a policy *was* set and we
            cannot enforce it — callers on the spawn path should fail closed.
    """
    if not path.is_file():
        return RolePolicy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyFileCorrupt(
            f"role_adapter_policy: policy file {path} exists but cannot be read or parsed: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PolicyFileCorrupt(f"role_adapter_policy: policy file {path} does not contain a JSON object")
    return RolePolicy.from_dict(data)


def save_policy_file(policy: RolePolicy, path: Path = DEFAULT_POLICY_PATH) -> Path:
    """Persist *policy* to *path* as canonical sorted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def enforce(
    role: str,
    adapter: str,
    *,
    audit_log: AuditLog | None = None,
    actor: str = "orchestrator",
    policy: RolePolicy | None = None,
) -> None:
    """Enforce the allow-list for *role* spawning *adapter*.

    Args:
        role: The effective role of the spawn site (``backend``, ``security``,
            etc.). Free-form string - unrecognised roles are treated as
            unrestricted for back-compat.
        adapter: Adapter id (e.g. ``claude``, ``aider``, ``mock``).
        audit_log: Optional :class:`AuditLog`. When provided, a deny emits a
            ``role.adapter.denied`` event into the HMAC chain.
        actor: Audit-event actor field. Defaults to ``"orchestrator"``.
        policy: Override the global policy (useful in tests).

    Raises:
        RoleAdapterDenied: If *adapter* is not on *role*'s allow-list.
    """
    effective = policy if policy is not None else get_policy()
    if effective.is_allowed(role, adapter):
        return

    allowed = effective.allowed_for(role)
    if audit_log is not None:
        try:
            audit_log.log(
                ADAPTER_DENY_EVENT_TYPE,
                actor,
                "adapter",
                adapter,
                {
                    "role": role,
                    "adapter": adapter,
                    "allowed_adapters": list(allowed),
                    "reason": "not in role allow-list",
                },
            )
        except Exception as exc:
            logger.warning("role_adapter_policy: audit emit failed for %s/%s - %s", role, adapter, exc)
    raise RoleAdapterDenied(role=role, adapter=adapter, allowed=allowed)


def check(role: str, adapter: str, *, policy: RolePolicy | None = None) -> bool:
    """Non-raising variant of :func:`enforce` for read-only callers.

    Returns True when the spawn would be allowed; False otherwise.
    """
    effective = policy if policy is not None else get_policy()
    return effective.is_allowed(role, adapter)
