"""One thin bridge contract between Bernstein and an external identity directory.

Operators who already run a directory that answers "who is allowed to do what"
for their people are asked the same question about their agents. This module is
the whole of Bernstein's side of that answer: a protocol with three operations
-- resolve a principal, list its group memberships, report a revocation -- and
a bridge that speaks only that protocol.

Nothing vendor-specific belongs here. A directory client is an adapter, it is
registered through :mod:`bernstein.core.security.directory_registry`, and
``bernstein.core`` never imports its SDK (``tests/unit/
test_core_has_no_directory_vendor_sdk.py`` enforces that statically). The
adapter surface is a :class:`typing.Protocol` rather than a base class, so an
adapter satisfies it structurally without importing Bernstein at all.

Every resolution is appended to the HMAC-chained audit log as
``identity.directory_resolution``. That is what turns "the directory said this
agent was in that group at that time" into a fact a reader can verify later
rather than a live lookup nobody can reproduce. Answers served from the bridge
cache are recorded too, carrying the moment the directory was actually asked
and the age of that answer, so a decision taken on a stale membership is
visible as such in the record instead of being indistinguishable from a fresh
one.

The bridge feeds RBAC, it does not bypass it: it maps groups to a role through
:func:`bernstein.core.security.rbac.resolve_role_from_groups` -- the same rule
the dashboard's OIDC login uses -- and the existing enforcement path decides
what that role may do.

Usage::

    bridge = DirectoryBridge(
        adapter=my_adapter,
        chain=AuditChainStore(audit_dir),
        role_mapping={"platform-admins": "admin"},
    )
    resolution = bridge.resolve("agent:packager")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.core.security.audit_chain import EVENT_DIRECTORY_RESOLUTION
from bernstein.core.security.rbac import resolve_role_from_groups

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

__all__ = [
    "FRESHNESS_CACHED",
    "FRESHNESS_FRESH",
    "DirectoryAdapter",
    "DirectoryBridge",
    "DirectoryBridgeError",
    "DirectoryPrincipal",
    "DirectoryResolution",
    "DirectoryRevocation",
]

FRESHNESS_FRESH = "fresh"
"""The directory itself answered during this resolution."""

FRESHNESS_CACHED = "cached"
"""Principal and group membership came from the bridge cache."""

_ACTOR_PREFIX = "directory"
_RESOURCE_TYPE = "principal"
_DEFAULT_CACHE_TTL_S = 300.0


class DirectoryBridgeError(RuntimeError):
    """Raised when the directory could not be consulted at all.

    A failed lookup is not a resolution: no chain event is written for it, so
    the record never claims the directory answered when it did not.
    """


@dataclass(frozen=True, slots=True)
class DirectoryPrincipal:
    """A principal as the external directory describes it.

    Attributes:
        principal_id: The directory's stable identifier for the principal.
        display_name: Human-readable name, empty when the directory has none.
        email: Contact address, empty when the directory has none.
        kind: ``"agent"`` or ``"human"``; adapters may use other values.
        attributes: Extra directory attributes, kept as opaque strings.
    """

    principal_id: str
    display_name: str = ""
    email: str = ""
    kind: str = "agent"
    attributes: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class DirectoryRevocation:
    """What the directory reports about a principal's revocation.

    Attributes:
        principal_id: The principal this answer is about.
        revoked: True when the directory no longer vouches for the principal.
        revoked_at: When the revocation took effect, when the directory says.
        reason: Free-form reason string, empty when the directory has none.
    """

    principal_id: str
    revoked: bool = False
    revoked_at: float | None = None
    reason: str = ""


@runtime_checkable
class DirectoryAdapter(Protocol):
    """The whole contract a directory adapter implements.

    An adapter is expected to be a thin translation of one vendor's API into
    these three operations plus its own ``name`` and ``version``, which are
    recorded with every resolution so a reader can tell which code produced a
    historical answer.
    """

    name: str
    version: str

    def resolve_principal(self, principal_ref: str) -> DirectoryPrincipal | None:
        """Return the principal ``principal_ref`` names, or None if unknown."""
        ...

    def list_groups(self, principal_id: str) -> tuple[str, ...]:
        """Return the group memberships of ``principal_id``."""
        ...

    def revocation(self, principal_id: str) -> DirectoryRevocation:
        """Return the revocation state of ``principal_id``."""
        ...


@dataclass(frozen=True, slots=True)
class DirectoryResolution:
    """One answer from the directory, exactly as it was recorded.

    Attributes:
        adapter: ``name`` of the adapter that produced the answer.
        adapter_version: ``version`` of that adapter.
        principal_ref: The reference the caller asked about.
        principal_id: The directory's identifier, empty when not found.
        found: Whether the directory knows ``principal_ref``.
        groups: Group memberships; empty for a revoked or unknown principal.
        role: Bernstein role mapped from ``groups``; empty when none applies.
        revoked: Whether the directory reports the principal as revoked.
        revoked_at: Revocation timestamp reported by the directory, if any.
        revocation_reason: Revocation reason reported by the directory, if any.
        freshness: :data:`FRESHNESS_FRESH` or :data:`FRESHNESS_CACHED`.
        observed_at: When the directory was actually asked.
        resolved_at: When this resolution was produced.
        age_s: ``resolved_at - observed_at``; zero for a fresh answer.
        ttl_s: The cache lifetime in force for this resolution.
    """

    adapter: str
    adapter_version: str
    principal_ref: str
    principal_id: str
    found: bool
    groups: tuple[str, ...]
    role: str
    revoked: bool
    revoked_at: float | None
    revocation_reason: str
    freshness: str
    observed_at: float
    resolved_at: float
    age_s: float
    ttl_s: float


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """A principal and its groups as observed at one moment."""

    principal: DirectoryPrincipal | None
    groups: tuple[str, ...]
    observed_at: float


@dataclass(slots=True)
class DirectoryBridge:
    """Resolve principals through one directory adapter and record every answer.

    Args:
        adapter: The directory adapter; only the protocol above is used.
        chain: Audit chain the resolutions are appended to.
        role_mapping: Directory group name to Bernstein role.
        cache_ttl_s: How long a principal and its groups may be reused before
            the directory is asked again. Revocation is never cached.
        clock: Time source, injectable for tests.
    """

    adapter: DirectoryAdapter
    chain: AuditChainStore
    role_mapping: Mapping[str, str] = field(default_factory=dict[str, str])
    cache_ttl_s: float = _DEFAULT_CACHE_TTL_S
    clock: Callable[[], float] = time.time
    _cache: dict[str, _CacheEntry] = field(default_factory=dict[str, _CacheEntry], init=False, repr=False)

    def resolve(self, principal_ref: str) -> DirectoryResolution:
        """Resolve ``principal_ref`` and append the answer to the audit chain.

        Args:
            principal_ref: How the caller names the principal (the adapter
                decides what a reference looks like for its directory).

        Returns:
            The :class:`DirectoryResolution` that was recorded.

        Raises:
            DirectoryBridgeError: When the adapter could not answer at all.
        """
        now = float(self.clock())
        cached = self._cache.get(principal_ref)
        if cached is not None and now - cached.observed_at <= self.cache_ttl_s:
            entry, freshness = cached, FRESHNESS_CACHED
        else:
            entry, freshness = self._observe(principal_ref, now), FRESHNESS_FRESH
            self._cache[principal_ref] = entry

        # Revocation is read on every resolution, cache hit included: a
        # membership may safely be a few minutes old, but serving a role to a
        # principal the directory has already disowned is the failure the
        # bridge exists to prevent.
        revocation = self._revocation(entry.principal)
        if revocation.revoked:
            self._cache.pop(principal_ref, None)

        resolution = self._build(principal_ref, entry, revocation, freshness, now)
        self._record(resolution)
        return resolution

    def invalidate(self, principal_ref: str) -> None:
        """Drop any cached answer for ``principal_ref``."""
        self._cache.pop(principal_ref, None)

    # -- internals ----------------------------------------------------------

    def _observe(self, principal_ref: str, now: float) -> _CacheEntry:
        """Ask the directory about ``principal_ref``."""
        try:
            principal = self.adapter.resolve_principal(principal_ref)
            groups = tuple(self.adapter.list_groups(principal.principal_id)) if principal is not None else ()
        except Exception as exc:
            msg = f"directory adapter {self.adapter.name!r} could not resolve {principal_ref!r}: {exc}"
            raise DirectoryBridgeError(msg) from exc
        return _CacheEntry(principal=principal, groups=groups, observed_at=now)

    def _revocation(self, principal: DirectoryPrincipal | None) -> DirectoryRevocation:
        """Ask the directory whether ``principal`` is revoked."""
        if principal is None:
            return DirectoryRevocation(principal_id="")
        try:
            return self.adapter.revocation(principal.principal_id)
        except Exception as exc:
            msg = (
                f"directory adapter {self.adapter.name!r} could not report revocation "
                f"for {principal.principal_id!r}: {exc}"
            )
            raise DirectoryBridgeError(msg) from exc

    def _build(
        self,
        principal_ref: str,
        entry: _CacheEntry,
        revocation: DirectoryRevocation,
        freshness: str,
        now: float,
    ) -> DirectoryResolution:
        """Assemble the resolution that will be recorded and returned."""
        found = entry.principal is not None
        usable = found and not revocation.revoked
        groups = entry.groups if usable else ()
        role = resolve_role_from_groups(groups, self.role_mapping) if usable else ""
        return DirectoryResolution(
            adapter=self.adapter.name,
            adapter_version=self.adapter.version,
            principal_ref=principal_ref,
            principal_id=entry.principal.principal_id if entry.principal is not None else "",
            found=found,
            groups=groups,
            role=role,
            revoked=revocation.revoked,
            revoked_at=revocation.revoked_at,
            revocation_reason=revocation.reason,
            freshness=freshness,
            observed_at=entry.observed_at,
            resolved_at=now,
            age_s=now - entry.observed_at,
            ttl_s=self.cache_ttl_s,
        )

    def _record(self, resolution: DirectoryResolution) -> None:
        """Append ``resolution`` to the audit chain."""
        self.chain.log_with_prev_digest(
            event_type=EVENT_DIRECTORY_RESOLUTION,
            actor=f"{_ACTOR_PREFIX}:{resolution.adapter}",
            resource_type=_RESOURCE_TYPE,
            resource_id=resolution.principal_id or resolution.principal_ref,
            details={
                "adapter": resolution.adapter,
                "adapter_version": resolution.adapter_version,
                "principal_ref": resolution.principal_ref,
                "principal_id": resolution.principal_id,
                "found": resolution.found,
                "groups": list(resolution.groups),
                "role": resolution.role,
                "revoked": resolution.revoked,
                "revoked_at": resolution.revoked_at,
                "revocation_reason": resolution.revocation_reason,
                "freshness": resolution.freshness,
                "observed_at": resolution.observed_at,
                "resolved_at": resolution.resolved_at,
                "age_s": resolution.age_s,
                "ttl_s": resolution.ttl_s,
            },
        )
