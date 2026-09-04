"""Tests for the external-directory bridge contract (issue #4970).

The bridge is one protocol -- resolve a principal, list its groups, report a
revocation -- that any directory adapter implements. Core speaks only that
protocol, and every resolution it performs is appended to the HMAC-chained
audit log so "the directory said this principal was in that group at that
time" survives as a historical fact rather than a live lookup nobody can
reproduce later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_DIRECTORY_RESOLUTION,
    AuditChainStore,
)
from bernstein.core.security.auth import AuthRole, AuthUser
from bernstein.core.security.directory_bridge import (
    FRESHNESS_CACHED,
    FRESHNESS_FRESH,
    DirectoryAdapter,
    DirectoryBridge,
    DirectoryBridgeError,
    DirectoryPrincipal,
    DirectoryRevocation,
)
from bernstein.core.security.rbac import RBACEnforcer, resolve_role_from_groups

_ROLE_MAPPING = {"platform-admins": "admin", "agent-operators": "operator"}


class _FakeDirectory:
    """In-memory stand-in for an external directory.

    Implements the bridge protocol structurally -- it subclasses nothing from
    ``bernstein`` -- which is the property an out-of-tree adapter relies on.
    """

    name = "fake-directory"
    version = "1.2.3"

    def __init__(
        self,
        principals: dict[str, DirectoryPrincipal] | None = None,
        groups: dict[str, tuple[str, ...]] | None = None,
        revoked: dict[str, DirectoryRevocation] | None = None,
    ) -> None:
        self.principals = principals or {}
        self.groups = groups or {}
        self.revoked = revoked or {}
        self.resolve_calls = 0

    def resolve_principal(self, principal_ref: str) -> DirectoryPrincipal | None:
        self.resolve_calls += 1
        return self.principals.get(principal_ref)

    def list_groups(self, principal_id: str) -> tuple[str, ...]:
        return self.groups.get(principal_id, ())

    def revocation(self, principal_id: str) -> DirectoryRevocation:
        return self.revoked.get(principal_id, DirectoryRevocation(principal_id=principal_id))


class _ExplodingDirectory:
    """Adapter whose backing directory is unreachable."""

    name = "exploding-directory"
    version = "0.0.1"

    def resolve_principal(self, principal_ref: str) -> DirectoryPrincipal | None:
        msg = f"directory unreachable while resolving {principal_ref}"
        raise TimeoutError(msg)

    def list_groups(self, principal_id: str) -> tuple[str, ...]:
        del principal_id
        return ()

    def revocation(self, principal_id: str) -> DirectoryRevocation:
        return DirectoryRevocation(principal_id=principal_id)


class _Clock:
    """Manually advanced clock."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _directory() -> _FakeDirectory:
    return _FakeDirectory(
        principals={
            "agent:packager": DirectoryPrincipal(
                principal_id="uid-7",
                display_name="Packager agent",
                email="packager@example.com",
                kind="agent",
            )
        },
        groups={"uid-7": ("agent-operators", "eu-region")},
    )


def _bridge(
    tmp_path: Path,
    *,
    directory: object | None = None,
    clock: _Clock | None = None,
    cache_ttl_s: float = 300.0,
) -> tuple[DirectoryBridge, AuditChainStore, _Clock]:
    chain = _store(tmp_path)
    tick = clock or _Clock()
    bridge = DirectoryBridge(
        adapter=directory or _directory(),  # type: ignore[arg-type]
        chain=chain,
        role_mapping=_ROLE_MAPPING,
        cache_ttl_s=cache_ttl_s,
        clock=tick,
    )
    return bridge, chain, tick


def test_fake_directory_adapter_satisfies_the_bridge_contract(tmp_path: Path) -> None:
    """A structurally-typed adapter resolves principal, groups and revocation."""
    directory = _directory()
    assert isinstance(directory, DirectoryAdapter)

    bridge, _chain, _clock = _bridge(tmp_path, directory=directory)
    resolution = bridge.resolve("agent:packager")

    assert resolution.found is True
    assert resolution.principal_id == "uid-7"
    assert resolution.groups == ("agent-operators", "eu-region")
    assert resolution.revoked is False
    assert resolution.adapter == "fake-directory"
    assert resolution.adapter_version == "1.2.3"
    assert resolution.role == "operator"


def test_every_resolution_appends_a_chain_event_naming_adapter_principal_and_groups(
    tmp_path: Path,
) -> None:
    """The load-bearing property: a resolution that is not recorded did not happen."""
    bridge, chain, clock = _bridge(tmp_path)

    bridge.resolve("agent:packager")
    clock.advance(10_000.0)
    bridge.resolve("agent:packager")

    events = chain.query(event_type=EVENT_DIRECTORY_RESOLUTION)
    assert len(events) == 2
    for event in events:
        assert event.resource_type == "principal"
        assert event.resource_id == "uid-7"
        assert event.details["adapter"] == "fake-directory"
        assert event.details["adapter_version"] == "1.2.3"
        assert event.details["principal_ref"] == "agent:packager"
        assert event.details["principal_id"] == "uid-7"
        assert event.details["groups"] == ["agent-operators", "eu-region"]
        assert event.details["role"] == "operator"
        assert "prev_chain_digest" in event.details

    ok, errors = chain.verify()
    assert ok, errors


def test_cached_resolution_is_distinguishable_from_a_fresh_one_in_the_record(
    tmp_path: Path,
) -> None:
    """A cached answer carries its age and the moment the directory was asked."""
    directory = _directory()
    bridge, chain, clock = _bridge(tmp_path, directory=directory, cache_ttl_s=300.0)

    bridge.resolve("agent:packager")
    clock.advance(60.0)
    cached = bridge.resolve("agent:packager")

    assert directory.resolve_calls == 1
    assert cached.freshness == FRESHNESS_CACHED
    assert cached.age_s == pytest.approx(60.0)
    assert cached.observed_at == pytest.approx(1_000.0)

    first, second = chain.query(event_type=EVENT_DIRECTORY_RESOLUTION)
    assert first.details["freshness"] == FRESHNESS_FRESH
    assert first.details["age_s"] == pytest.approx(0.0)
    assert second.details["freshness"] == FRESHNESS_CACHED
    assert second.details["age_s"] == pytest.approx(60.0)
    assert second.details["observed_at"] == pytest.approx(1_000.0)
    assert second.details["resolved_at"] == pytest.approx(1_060.0)
    assert second.details["ttl_s"] == pytest.approx(300.0)


def test_expired_cache_entry_is_resolved_against_the_directory_again(tmp_path: Path) -> None:
    """Past the TTL the bridge asks the directory rather than serving the cache."""
    directory = _directory()
    bridge, chain, clock = _bridge(tmp_path, directory=directory, cache_ttl_s=300.0)

    bridge.resolve("agent:packager")
    clock.advance(301.0)
    refreshed = bridge.resolve("agent:packager")

    assert directory.resolve_calls == 2
    assert refreshed.freshness == FRESHNESS_FRESH
    assert refreshed.age_s == pytest.approx(0.0)
    assert [e.details["freshness"] for e in chain.query(event_type=EVENT_DIRECTORY_RESOLUTION)] == [
        FRESHNESS_FRESH,
        FRESHNESS_FRESH,
    ]


def test_revoked_principal_resolves_to_no_role_and_no_groups(tmp_path: Path) -> None:
    """A revocation the directory reports strips the role rather than annotating it."""
    directory = _directory()
    directory.revoked["uid-7"] = DirectoryRevocation(
        principal_id="uid-7",
        revoked=True,
        revoked_at=900.0,
        reason="offboarded",
    )
    bridge, chain, _clock = _bridge(tmp_path, directory=directory)

    resolution = bridge.resolve("agent:packager")

    assert resolution.revoked is True
    assert resolution.groups == ()
    assert resolution.role == ""
    event = chain.query(event_type=EVENT_DIRECTORY_RESOLUTION)[0]
    assert event.details["revoked"] is True
    assert event.details["revoked_at"] == pytest.approx(900.0)
    assert event.details["revocation_reason"] == "offboarded"
    assert event.details["groups"] == []


def test_revoked_principal_is_never_served_from_cache(tmp_path: Path) -> None:
    """A revocation observed once must not be papered over by an older cached answer."""
    directory = _directory()
    bridge, _chain, clock = _bridge(tmp_path, directory=directory, cache_ttl_s=300.0)

    assert bridge.resolve("agent:packager").role == "operator"
    directory.revoked["uid-7"] = DirectoryRevocation(principal_id="uid-7", revoked=True)
    clock.advance(1.0)

    assert bridge.resolve("agent:packager").revoked is True
    assert directory.resolve_calls == 1  # revocation is re-read even on a cache hit


def test_unknown_principal_is_recorded_as_not_found(tmp_path: Path) -> None:
    """A principal the directory does not know is a recorded answer, not silence."""
    bridge, chain, _clock = _bridge(tmp_path)

    resolution = bridge.resolve("agent:ghost")

    assert resolution.found is False
    assert resolution.principal_id == ""
    assert resolution.role == ""
    event = chain.query(event_type=EVENT_DIRECTORY_RESOLUTION)[0]
    assert event.details["found"] is False
    assert event.details["principal_ref"] == "agent:ghost"
    assert event.resource_id == "agent:ghost"


def test_unreachable_directory_raises_and_records_nothing(tmp_path: Path) -> None:
    """A failed lookup is not a resolution, so it must not enter the record as one."""
    bridge, chain, _clock = _bridge(tmp_path, directory=_ExplodingDirectory())

    with pytest.raises(DirectoryBridgeError):
        bridge.resolve("agent:packager")

    assert chain.query(event_type=EVENT_DIRECTORY_RESOLUTION) == []


def test_resolved_role_feeds_rbac_instead_of_bypassing_it(tmp_path: Path) -> None:
    """The bridge produces a role; RBAC still decides what that role may do."""
    bridge, _chain, _clock = _bridge(tmp_path)
    resolution = bridge.resolve("agent:packager")

    user = AuthUser(
        id=resolution.principal_id,
        email="packager@example.com",
        display_name="Packager agent",
        role=AuthRole(resolution.role),
        sso_groups=list(resolution.groups),
    )
    enforcer = RBACEnforcer()

    allowed, _reason = enforcer.check_access(user, "/tasks", "POST")
    assert allowed is True
    denied, reason = enforcer.check_access(user, "/auth/users", "POST")
    assert denied is False
    assert "auth:manage" in reason


def test_group_role_mapping_is_the_same_rule_the_oidc_provider_uses(tmp_path: Path) -> None:
    """One mapping rule, owned by rbac, serves both the dashboard and the bridge."""
    from bernstein.core.security.sso_oidc import OIDCConfig, OIDCProvider

    groups = ["eu-region", "platform-admins"]
    provider = OIDCProvider(OIDCConfig(role_mapping=dict(_ROLE_MAPPING)))

    assert provider.resolve_role(groups) == resolve_role_from_groups(groups, _ROLE_MAPPING)

    directory = _FakeDirectory(
        principals={"human:root": DirectoryPrincipal(principal_id="uid-1", kind="human")},
        groups={"uid-1": tuple(groups)},
    )
    bridge, _chain, _clock = _bridge(tmp_path, directory=directory)
    assert bridge.resolve("human:root").role == "admin"
