"""Dispatch-path tests for capability-aware routing (issue #2663).

The audit-chain primitives and the ``route_and_record`` seam are proven in
``tests/unit/adapters/test_capability_profile.py`` in isolation. These tests
prove the missing half: that the *spawn dispatch path* invokes the seam, so a
profile hash is anchored at dispatch (AC3) and a task whose declared capability
requirements outrun the routed adapter is refused with a signed receipt rather
than silently spawned on a weaker adapter (AC2).

The hook is exercised directly on a real :class:`AgentSpawner` so the test
asserts the chain the production dispatch path writes, not a stand-in. The
audit key is isolated to ``tmp_path`` by the autouse ``_isolate_audit_key``
conftest fixture, so the chain is hermetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import Task
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.capability_profile import CapabilityMismatchError
from bernstein.core.security.audit_chain import (
    EVENT_ADAPTER_CAPABILITY_REFUSAL,
    EVENT_ADAPTER_CAPABILITY_SELECTION,
    AuditChainStore,
)

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock


def _spawner(tmp_path: Path, adapter: MagicMock) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False, default_model="mock-model")


def _task(*, requires: list[str] | None = None) -> Task:
    return Task(
        id="T-cap-1",
        title="Capability routing task",
        description="Exercise dispatch-time capability routing.",
        role="backend",
        requires=requires or [],
    )


def _chain(tmp_path: Path) -> AuditChainStore:
    # Same audit dir the spawner hook writes to; the isolated key is loaded
    # from the conftest-set BERNSTEIN_AUDIT_KEY_PATH, so a re-open reads it.
    return AuditChainStore(tmp_path / ".sdd" / "audit")


class TestDispatchRecordsPresentedProfileHash:
    """AC3: the hash a profiled adapter presents at dispatch is anchored."""

    def test_profiled_selection_is_recorded_at_dispatch(self, tmp_path: Path, mock_adapter_factory) -> None:
        from bernstein.adapters.capability_profile import PROFILES

        spawner = _spawner(tmp_path, mock_adapter_factory())
        spawner._record_adapter_capability_selection("opencode", [_task()])

        events = _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_SELECTION)
        assert len(events) == 1
        # The recorded hash is the routed adapter's own content address.
        assert events[0].details["profile_hash"] == PROFILES["opencode"].profile_hash
        assert events[0].details["adapter"] == "opencode"
        assert events[0].details["run_id"] == "T-cap-1"
        # No refusal on a satisfied route.
        assert _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_REFUSAL) == []

    def test_met_declared_requirement_records_selection(self, tmp_path: Path, mock_adapter_factory) -> None:
        # goose declares mcp_client=True, so a task requiring it is satisfied.
        spawner = _spawner(tmp_path, mock_adapter_factory())
        spawner._record_adapter_capability_selection("goose", [_task(requires=["capability:mcp_client"])])
        assert len(_chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_SELECTION)) == 1
        assert _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_REFUSAL) == []

    def test_unprofiled_adapter_is_a_noop(self, tmp_path: Path, mock_adapter_factory) -> None:
        # The common claude/codex/gemini path carries no profile: the generic
        # fallback owns it, and nothing capability-related is anchored.
        spawner = _spawner(tmp_path, mock_adapter_factory())
        spawner._record_adapter_capability_selection("claude", [_task()])
        assert _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_SELECTION) == []
        assert _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_REFUSAL) == []


class TestDispatchRefusesOnCapabilityMismatch:
    """AC2: an unmet declared requirement is a signed refusal, not a fallback."""

    def test_mismatch_raises_and_anchors_refusal(self, tmp_path: Path, mock_adapter_factory) -> None:
        # opencode declares vision=False; a task requiring vision cannot route
        # to it, so the dispatch path refuses before any spawn.
        adapter = mock_adapter_factory()
        spawner = _spawner(tmp_path, adapter)
        with pytest.raises(CapabilityMismatchError):
            spawner._record_adapter_capability_selection("opencode", [_task(requires=["capability:vision"])])

        refusals = _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_REFUSAL)
        assert len(refusals) == 1
        assert refusals[0].details["unmet"] == ["vision"]
        # No selection is recorded for a refused route (no silent fallback).
        assert _chain(tmp_path).query(event_type=EVENT_ADAPTER_CAPABILITY_SELECTION) == []
        # The refusal is a hard stop: the adapter is never spawned.
        adapter.spawn.assert_not_called()

    def test_malformed_declaration_fails_loud(self, tmp_path: Path, mock_adapter_factory) -> None:
        from bernstein.adapters.capability_profile import ProfileValidationError

        spawner = _spawner(tmp_path, mock_adapter_factory())
        with pytest.raises(ProfileValidationError):
            spawner._record_adapter_capability_selection("opencode", [_task(requires=["capability:teleportation"])])
