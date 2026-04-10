"""Tests for heartbeat protocol v2 (road-055)."""

from __future__ import annotations

import time

import pytest

from bernstein.core.heartbeat_v2 import (
    AgentHealth,
    AgentPong,
    HealthState,
    HeartbeatV2Manager,
    OrchestratorPing,
    PingType,
)

# ---------------------------------------------------------------------------
# PingType enum
# ---------------------------------------------------------------------------


class TestPingType:
    def test_members(self) -> None:
        assert PingType.STATUS_REQUEST.value == "status_request"
        assert PingType.PROGRESS_REQUEST.value == "progress_request"
        assert PingType.CONTEXT_SIZE_REQUEST.value == "context_size_request"
        assert PingType.ETA_REQUEST.value == "eta_request"

    def test_all_members_count(self) -> None:
        assert len(PingType) == 4


# ---------------------------------------------------------------------------
# OrchestratorPing dataclass
# ---------------------------------------------------------------------------


class TestOrchestratorPing:
    def test_frozen(self) -> None:
        ping = OrchestratorPing(
            ping_id="p1",
            ping_type=PingType.STATUS_REQUEST,
            agent_id="a1",
            timestamp=1.0,
        )
        with pytest.raises(AttributeError):
            ping.ping_id = "p2"  # type: ignore[misc]

    def test_default_timeout(self) -> None:
        ping = OrchestratorPing(
            ping_id="p1",
            ping_type=PingType.STATUS_REQUEST,
            agent_id="a1",
            timestamp=1.0,
        )
        assert ping.timeout_s == 10.0

    def test_custom_timeout(self) -> None:
        ping = OrchestratorPing(
            ping_id="p1",
            ping_type=PingType.STATUS_REQUEST,
            agent_id="a1",
            timestamp=1.0,
            timeout_s=5.0,
        )
        assert ping.timeout_s == 5.0


# ---------------------------------------------------------------------------
# AgentPong dataclass
# ---------------------------------------------------------------------------


class TestAgentPong:
    def test_frozen(self) -> None:
        pong = AgentPong(
            ping_id="p1",
            agent_id="a1",
            status="running",
            progress_pct=50.0,
            context_tokens=5000,
            estimated_remaining_s=30.0,
            timestamp=2.0,
        )
        with pytest.raises(AttributeError):
            pong.status = "done"  # type: ignore[misc]

    def test_fields(self) -> None:
        pong = AgentPong(
            ping_id="p1",
            agent_id="a1",
            status="idle",
            progress_pct=0.0,
            context_tokens=0,
            estimated_remaining_s=0.0,
            timestamp=1.0,
        )
        assert pong.ping_id == "p1"
        assert pong.agent_id == "a1"
        assert pong.context_tokens == 0


# ---------------------------------------------------------------------------
# HealthState enum
# ---------------------------------------------------------------------------


class TestHealthState:
    def test_members(self) -> None:
        assert HealthState.HEALTHY.value == "healthy"
        assert HealthState.DEGRADED.value == "degraded"
        assert HealthState.UNRESPONSIVE.value == "unresponsive"

    def test_all_members_count(self) -> None:
        assert len(HealthState) == 3


# ---------------------------------------------------------------------------
# AgentHealth dataclass
# ---------------------------------------------------------------------------


class TestAgentHealth:
    def test_frozen(self) -> None:
        health = AgentHealth(
            agent_id="a1",
            state=HealthState.HEALTHY,
            last_ping_at=1.0,
            last_pong_at=1.1,
            response_time_ms=100.0,
            consecutive_failures=0,
        )
        with pytest.raises(AttributeError):
            health.state = HealthState.DEGRADED  # type: ignore[misc]

    def test_optional_fields_none(self) -> None:
        health = AgentHealth(
            agent_id="a1",
            state=HealthState.HEALTHY,
            last_ping_at=0.0,
            last_pong_at=None,
            response_time_ms=None,
            consecutive_failures=0,
        )
        assert health.last_pong_at is None
        assert health.response_time_ms is None


# ---------------------------------------------------------------------------
# HeartbeatV2Manager
# ---------------------------------------------------------------------------


class TestHeartbeatV2Manager:
    def test_create_ping_returns_orchestrator_ping(self) -> None:
        mgr = HeartbeatV2Manager()
        ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        assert isinstance(ping, OrchestratorPing)
        assert ping.agent_id == "a1"
        assert ping.ping_type is PingType.STATUS_REQUEST
        assert len(ping.ping_id) == 32  # uuid4 hex

    def test_create_ping_unique_ids(self) -> None:
        mgr = HeartbeatV2Manager()
        p1 = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        p2 = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        assert p1.ping_id != p2.ping_id

    def test_create_ping_custom_timeout(self) -> None:
        mgr = HeartbeatV2Manager(timeout_s=10.0)
        ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1", timeout_s=3.0)
        assert ping.timeout_s == 3.0

    def test_create_ping_default_timeout(self) -> None:
        mgr = HeartbeatV2Manager(timeout_s=7.0)
        ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        assert ping.timeout_s == 7.0

    def test_record_pong_resets_failures(self) -> None:
        mgr = HeartbeatV2Manager(degraded_threshold=1, dead_threshold=3)
        ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        mgr.record_timeout("a1", ping.ping_id)
        assert mgr.get_health("a1").consecutive_failures == 1

        ping2 = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        pong = AgentPong(
            ping_id=ping2.ping_id,
            agent_id="a1",
            status="running",
            progress_pct=10.0,
            context_tokens=1000,
            estimated_remaining_s=60.0,
            timestamp=time.time(),
        )
        mgr.record_pong(pong)
        assert mgr.get_health("a1").consecutive_failures == 0
        assert mgr.get_health("a1").state is HealthState.HEALTHY

    def test_record_pong_tracks_rtt(self) -> None:
        mgr = HeartbeatV2Manager()
        ping = mgr.create_ping(PingType.PROGRESS_REQUEST, "a1")
        pong = AgentPong(
            ping_id=ping.ping_id,
            agent_id="a1",
            status="running",
            progress_pct=50.0,
            context_tokens=8000,
            estimated_remaining_s=20.0,
            timestamp=ping.timestamp + 0.05,  # 50ms later
        )
        mgr.record_pong(pong)
        health = mgr.get_health("a1")
        assert health.response_time_ms is not None
        assert health.response_time_ms == pytest.approx(50.0, abs=1.0)

    def test_record_timeout_increments_failures(self) -> None:
        mgr = HeartbeatV2Manager()
        ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        mgr.record_timeout("a1", ping.ping_id)
        assert mgr.get_health("a1").consecutive_failures == 1

    def test_health_transitions_to_degraded(self) -> None:
        mgr = HeartbeatV2Manager(degraded_threshold=2, dead_threshold=5)
        for _ in range(2):
            ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
            mgr.record_timeout("a1", ping.ping_id)
        assert mgr.get_health("a1").state is HealthState.DEGRADED

    def test_health_transitions_to_unresponsive(self) -> None:
        mgr = HeartbeatV2Manager(degraded_threshold=2, dead_threshold=5)
        for _ in range(5):
            ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
            mgr.record_timeout("a1", ping.ping_id)
        assert mgr.get_health("a1").state is HealthState.UNRESPONSIVE

    def test_unknown_agent_is_healthy(self) -> None:
        mgr = HeartbeatV2Manager()
        health = mgr.get_health("unknown-agent")
        assert health.state is HealthState.HEALTHY
        assert health.consecutive_failures == 0

    def test_get_all_health(self) -> None:
        mgr = HeartbeatV2Manager()
        mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        mgr.create_ping(PingType.STATUS_REQUEST, "a2")
        all_health = mgr.get_all_health()
        assert set(all_health.keys()) == {"a1", "a2"}
        assert all(isinstance(h, AgentHealth) for h in all_health.values())

    def test_get_degraded_agents_empty(self) -> None:
        mgr = HeartbeatV2Manager()
        mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        assert mgr.get_degraded_agents() == []

    def test_get_degraded_agents_returns_only_unhealthy(self) -> None:
        mgr = HeartbeatV2Manager(degraded_threshold=1, dead_threshold=3)
        # a1 will be degraded
        ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
        mgr.record_timeout("a1", ping.ping_id)
        # a2 stays healthy
        mgr.create_ping(PingType.STATUS_REQUEST, "a2")

        degraded = mgr.get_degraded_agents()
        assert len(degraded) == 1
        assert degraded[0].agent_id == "a1"
        assert degraded[0].state is HealthState.DEGRADED

    def test_get_degraded_agents_includes_unresponsive(self) -> None:
        mgr = HeartbeatV2Manager(degraded_threshold=1, dead_threshold=2)
        for _ in range(2):
            ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
            mgr.record_timeout("a1", ping.ping_id)
        degraded = mgr.get_degraded_agents()
        assert len(degraded) == 1
        assert degraded[0].state is HealthState.UNRESPONSIVE

    def test_pong_for_unknown_ping_still_updates_health(self) -> None:
        mgr = HeartbeatV2Manager()
        pong = AgentPong(
            ping_id="nonexistent",
            agent_id="a1",
            status="running",
            progress_pct=75.0,
            context_tokens=4000,
            estimated_remaining_s=10.0,
            timestamp=time.time(),
        )
        mgr.record_pong(pong)
        health = mgr.get_health("a1")
        assert health.state is HealthState.HEALTHY
        assert health.last_pong_at is not None

    def test_last_ping_at_updated_on_create(self) -> None:
        mgr = HeartbeatV2Manager()
        before = time.time()
        mgr.create_ping(PingType.ETA_REQUEST, "a1")
        after = time.time()
        health = mgr.get_health("a1")
        assert before <= health.last_ping_at <= after

    def test_multiple_agents_independent(self) -> None:
        mgr = HeartbeatV2Manager(degraded_threshold=2, dead_threshold=4)
        # Degrade a1
        for _ in range(2):
            ping = mgr.create_ping(PingType.STATUS_REQUEST, "a1")
            mgr.record_timeout("a1", ping.ping_id)
        # a2 stays healthy
        ping2 = mgr.create_ping(PingType.STATUS_REQUEST, "a2")
        pong2 = AgentPong(
            ping_id=ping2.ping_id,
            agent_id="a2",
            status="running",
            progress_pct=100.0,
            context_tokens=200,
            estimated_remaining_s=0.0,
            timestamp=time.time(),
        )
        mgr.record_pong(pong2)

        assert mgr.get_health("a1").state is HealthState.DEGRADED
        assert mgr.get_health("a2").state is HealthState.HEALTHY

    def test_context_size_request_ping_type(self) -> None:
        mgr = HeartbeatV2Manager()
        ping = mgr.create_ping(PingType.CONTEXT_SIZE_REQUEST, "a1")
        assert ping.ping_type is PingType.CONTEXT_SIZE_REQUEST
