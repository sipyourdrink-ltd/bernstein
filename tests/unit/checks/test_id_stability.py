"""Tests pinning check ID stability, namespacing, uniqueness, and tombstones (#5072).

Pins the invariant that check IDs are stable across releases, namespaced by area,
never reused, and cannot be removed without an explicit tombstone.
"""

from __future__ import annotations

import pytest

from bernstein.core.checks.contract import Evidence, Finding, Verdict
from bernstein.core.checks.registry import CheckRegistry, populate_default_checks

# ---------------------------------------------------------------------------
# Canonical ID stability catalogue & tombstones
# ---------------------------------------------------------------------------

# Active stable check IDs pinned across releases
ACTIVE_PINNED_IDS: frozenset[str] = frozenset(
    {
        "doctor:compliance",
        "compliance:soc2:encryption_at_rest",
    }
)

# Retired / deprecated check IDs that may never be reused.
# As checks are deprecated and retired from the active catalogue across releases,
# their IDs must be pinned here as permanent tombstones so they are never reassigned.
TOMBSTONED_IDS: frozenset[str] = frozenset()


class _DummyCheck:
    def __init__(self, check_id: str) -> None:
        self.check_id = check_id

    def run(self, workdir=None) -> Finding:
        ev = Evidence(locator="dummy", sha256="sha256:1111222233334444")
        return Finding(check_id=self.check_id, verdict=Verdict.PASS, evidence=(ev,))


def test_active_pinned_ids_are_present_in_default_registry() -> None:
    """All active pinned IDs must be present when default checks are populated."""
    registry = CheckRegistry()
    populate_default_checks(registry)

    registered_ids = {c.check_id for c in registry.iter_checks()}
    for pinned_id in ACTIVE_PINNED_IDS:
        assert pinned_id in registered_ids, f"Pinned check ID '{pinned_id}' missing from populated registry"


def test_check_ids_are_properly_namespaced() -> None:
    """Check IDs must be non-empty and strictly namespaced with a colon."""
    registry = CheckRegistry()
    populate_default_checks(registry)

    for check in registry.iter_checks():
        cid = check.check_id
        assert isinstance(cid, str) and cid.strip() == cid
        assert ":" in cid, f"Check ID '{cid}' is not namespaced with a colon"
        assert not cid.startswith(":"), f"Check ID '{cid}' has leading colon"
        assert not cid.endswith(":"), f"Check ID '{cid}' has trailing colon"
        area, name = cid.split(":", 1)
        assert area, f"Check ID '{cid}' has empty area namespace"
        assert name, f"Check ID '{cid}' has empty name suffix"


def test_populated_registry_contains_no_tombstoned_ids() -> None:
    """The default populated registry and active pinned set must never contain tombstoned IDs."""
    registry = CheckRegistry()
    populate_default_checks(registry)

    registered_ids = {c.check_id for c in registry.iter_checks()}
    assert registered_ids & TOMBSTONED_IDS == set(), f"Tombstoned IDs found in active registry: {registered_ids & TOMBSTONED_IDS}"
    assert ACTIVE_PINNED_IDS & TOMBSTONED_IDS == set(), f"Pinned IDs overlap with tombstones: {ACTIVE_PINNED_IDS & TOMBSTONED_IDS}"


def test_registered_check_ids_are_unique() -> None:
    """No two checks can register with the same check_id."""
    registry = CheckRegistry()
    populate_default_checks(registry)

    check_ids = [c.check_id for c in registry.iter_checks()]
    assert len(check_ids) == len(set(check_ids)), f"Duplicate check IDs found: {check_ids}"


def test_duplicate_registration_raises_error() -> None:
    """Registering a check with an existing ID must raise a ValueError."""
    registry = CheckRegistry()
    check1 = _DummyCheck("doctor:compliance")
    registry.register(check1)

    check2 = _DummyCheck("doctor:compliance")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(check2)


def test_registering_check_class_instead_of_instance_raises_type_error() -> None:
    """Passing a check class object instead of an instance must raise a TypeError."""
    registry = CheckRegistry()
    with pytest.raises(TypeError, match="Expected Check instance, got class '_DummyCheck'"):
        registry.register(_DummyCheck)  # type: ignore[arg-type]

