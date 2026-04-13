"""Tests for bernstein.core.rolling_restart."""

from __future__ import annotations

import json
import os
import time
from typing import Any, cast
from unittest.mock import patch

import pytest
from bernstein.core.rolling_restart import (
    RestartPhase,
    RestartPlan,
    RestartState,
    deserialize_restart_state,
    format_restart_status,
    prepare_restart,
    serialize_restart_state,
    validate_handoff,
)

# ---------------------------------------------------------------------------
# RestartPhase enum
# ---------------------------------------------------------------------------


class TestRestartPhase:
    """RestartPhase enum basics."""

    def test_all_phases_present(self) -> None:
        expected = {"PREPARING", "DRAINING", "HANDOFF", "RESUMING", "COMPLETE", "FAILED"}
        assert {p.name for p in RestartPhase} == expected

    def test_string_values(self) -> None:
        assert RestartPhase.PREPARING == "preparing"
        assert RestartPhase.FAILED == "failed"

    def test_is_str(self) -> None:
        assert isinstance(RestartPhase.DRAINING, str)


# ---------------------------------------------------------------------------
# RestartState dataclass
# ---------------------------------------------------------------------------


class TestRestartState:
    """RestartState frozen dataclass."""

    def test_frozen(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=100,
            new_pid=0,
            wal_position=42,
        )
        with pytest.raises(AttributeError):
            state.phase = RestartPhase.DRAINING  # type: ignore[misc]

    def test_defaults(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=1,
            new_pid=0,
            wal_position=0,
        )
        assert state.active_agent_pids == []
        assert state.started_at == pytest.approx(0.0)
        assert state.completed_at == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# RestartPlan dataclass
# ---------------------------------------------------------------------------


class TestRestartPlan:
    """RestartPlan defaults and immutability."""

    def test_defaults(self) -> None:
        plan = RestartPlan()
        assert plan.drain_timeout_s == pytest.approx(30.0)
        assert plan.handoff_timeout_s == pytest.approx(10.0)
        assert plan.verify_agents is True
        assert plan.backup_state is True

    def test_custom_values(self) -> None:
        plan = RestartPlan(drain_timeout_s=60, handoff_timeout_s=5, verify_agents=False)
        assert plan.drain_timeout_s == 60
        assert plan.handoff_timeout_s == 5
        assert plan.verify_agents is False

    def test_frozen(self) -> None:
        plan = RestartPlan()
        with pytest.raises(AttributeError):
            plan.drain_timeout_s = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# prepare_restart
# ---------------------------------------------------------------------------


class TestPrepareRestart:
    """prepare_restart function."""

    def test_basic_prepare(self) -> None:
        plan = RestartPlan(verify_agents=False)
        state = prepare_restart(
            current_pid=1234,
            wal_position=99,
            agent_pids=[10, 20, 30],
            plan=plan,
        )
        assert state.phase == RestartPhase.PREPARING
        assert state.old_pid == 1234
        assert state.new_pid == 0
        assert state.wal_position == 99
        assert state.active_agent_pids == [10, 20, 30]
        assert state.started_at > 0

    def test_verify_agents_filters_dead_pids(self) -> None:
        plan = RestartPlan(verify_agents=True)
        # Use current PID (alive) and a very unlikely PID (dead).
        alive_pid = os.getpid()
        dead_pid = 2_147_483_647  # max 32-bit PID; almost certainly unused
        state = prepare_restart(
            current_pid=alive_pid,
            wal_position=0,
            agent_pids=[alive_pid, dead_pid],
            plan=plan,
        )
        assert alive_pid in state.active_agent_pids
        # dead_pid should have been filtered out
        assert dead_pid not in state.active_agent_pids

    def test_empty_agents(self) -> None:
        plan = RestartPlan(verify_agents=False)
        state = prepare_restart(current_pid=1, wal_position=0, agent_pids=[], plan=plan)
        assert state.active_agent_pids == []

    def test_verify_agents_disabled_keeps_all(self) -> None:
        plan = RestartPlan(verify_agents=False)
        state = prepare_restart(
            current_pid=1,
            wal_position=0,
            agent_pids=[99999, 99998],
            plan=plan,
        )
        assert state.active_agent_pids == [99999, 99998]


# ---------------------------------------------------------------------------
# validate_handoff
# ---------------------------------------------------------------------------


class TestValidateHandoff:
    """validate_handoff diagnostics."""

    def test_valid_preparing_state(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=100,
            new_pid=0,
            wal_position=1,
            started_at=time.time(),
        )
        assert validate_handoff(state) == []

    def test_invalid_old_pid(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=0,
            new_pid=0,
            wal_position=1,
            started_at=time.time(),
        )
        errors = validate_handoff(state)
        assert any("old_pid" in e for e in errors)

    def test_handoff_without_new_pid(self) -> None:
        state = RestartState(
            phase=RestartPhase.HANDOFF,
            old_pid=100,
            new_pid=0,
            wal_position=1,
            started_at=time.time(),
        )
        errors = validate_handoff(state)
        assert any("new_pid" in e for e in errors)

    def test_complete_without_completed_at(self) -> None:
        state = RestartState(
            phase=RestartPhase.COMPLETE,
            old_pid=100,
            new_pid=200,
            wal_position=1,
            started_at=time.time(),
            completed_at=0.0,
        )
        errors = validate_handoff(state)
        assert any("completed_at" in e for e in errors)

    def test_complete_valid(self) -> None:
        now = time.time()
        state = RestartState(
            phase=RestartPhase.COMPLETE,
            old_pid=100,
            new_pid=200,
            wal_position=1,
            started_at=now,
            completed_at=now + 1,
        )
        assert validate_handoff(state) == []

    def test_resuming_requires_new_pid(self) -> None:
        state = RestartState(
            phase=RestartPhase.RESUMING,
            old_pid=100,
            new_pid=0,
            wal_position=1,
            started_at=time.time(),
        )
        errors = validate_handoff(state)
        assert any("new_pid" in e for e in errors)

    def test_negative_started_at(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=100,
            new_pid=0,
            wal_position=1,
            started_at=-1.0,
        )
        errors = validate_handoff(state)
        assert any("started_at" in e for e in errors)


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    """serialize_restart_state / deserialize_restart_state."""

    def _make_state(self) -> RestartState:
        return RestartState(
            phase=RestartPhase.DRAINING,
            old_pid=42,
            new_pid=0,
            wal_position=7,
            active_agent_pids=[10, 20],
            started_at=1700000000.0,
            completed_at=0.0,
        )

    def test_round_trip(self) -> None:
        original = self._make_state()
        data = serialize_restart_state(original)
        restored = deserialize_restart_state(data)
        assert restored is not None
        assert restored.phase == original.phase
        assert restored.old_pid == original.old_pid
        assert restored.new_pid == original.new_pid
        assert restored.wal_position == original.wal_position
        assert restored.active_agent_pids == original.active_agent_pids
        assert restored.started_at == original.started_at
        assert restored.completed_at == original.completed_at

    def test_serialize_is_json(self) -> None:
        data = serialize_restart_state(self._make_state())
        parsed = json.loads(data)
        assert parsed["phase"] == "draining"
        assert parsed["old_pid"] == 42

    def test_deserialize_invalid_json(self) -> None:
        assert deserialize_restart_state("not json") is None

    def test_deserialize_missing_key(self) -> None:
        assert deserialize_restart_state('{"phase": "draining"}') is None

    def test_deserialize_bad_phase(self) -> None:
        data = serialize_restart_state(self._make_state())
        obj = json.loads(data)
        obj["phase"] = "nonexistent"
        assert deserialize_restart_state(json.dumps(obj)) is None

    def test_deserialize_none_input(self) -> None:
        assert deserialize_restart_state(cast(Any, None)) is None

    def test_all_phases_round_trip(self) -> None:
        for phase in RestartPhase:
            state = RestartState(
                phase=phase,
                old_pid=1,
                new_pid=2 if phase in {RestartPhase.HANDOFF, RestartPhase.RESUMING, RestartPhase.COMPLETE} else 0,
                wal_position=0,
                started_at=1.0,
                completed_at=2.0 if phase == RestartPhase.COMPLETE else 0.0,
            )
            restored = deserialize_restart_state(serialize_restart_state(state))
            assert restored is not None
            assert restored.phase == phase


# ---------------------------------------------------------------------------
# format_restart_status
# ---------------------------------------------------------------------------


class TestFormatRestartStatus:
    """format_restart_status human-readable output."""

    def test_contains_phase(self) -> None:
        state = RestartState(
            phase=RestartPhase.DRAINING,
            old_pid=10,
            new_pid=0,
            wal_position=5,
            started_at=time.time(),
        )
        output = format_restart_status(state)
        assert "DRAINING" in output

    def test_pending_new_pid(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=10,
            new_pid=0,
            wal_position=0,
            started_at=time.time(),
        )
        output = format_restart_status(state)
        assert "(pending)" in output

    def test_shows_new_pid_when_set(self) -> None:
        state = RestartState(
            phase=RestartPhase.HANDOFF,
            old_pid=10,
            new_pid=20,
            wal_position=0,
            started_at=time.time(),
        )
        output = format_restart_status(state)
        assert "20" in output
        assert "(pending)" not in output

    def test_agent_count(self) -> None:
        state = RestartState(
            phase=RestartPhase.DRAINING,
            old_pid=10,
            new_pid=0,
            wal_position=0,
            active_agent_pids=[1, 2, 3],
            started_at=time.time(),
        )
        output = format_restart_status(state)
        assert "3" in output

    def test_elapsed_zero_when_no_start(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=10,
            new_pid=0,
            wal_position=0,
            started_at=0.0,
        )
        output = format_restart_status(state)
        assert "0.0s" in output

    def test_shows_wal_position(self) -> None:
        state = RestartState(
            phase=RestartPhase.PREPARING,
            old_pid=10,
            new_pid=0,
            wal_position=999,
            started_at=time.time(),
        )
        output = format_restart_status(state)
        assert "999" in output


# ---------------------------------------------------------------------------
# _filter_alive (internal helper, tested indirectly + directly)
# ---------------------------------------------------------------------------


class TestFilterAlive:
    """Internal _filter_alive helper."""

    def test_current_pid_is_alive(self) -> None:
        from bernstein.core.rolling_restart import _filter_alive

        result = _filter_alive([os.getpid()])
        assert os.getpid() in result

    def test_nonexistent_pid_filtered(self) -> None:
        from bernstein.core.rolling_restart import _filter_alive

        result = _filter_alive([2_147_483_647])
        assert result == []

    def test_permission_error_counts_as_alive(self) -> None:
        from bernstein.core.rolling_restart import _filter_alive

        with patch("os.kill", side_effect=PermissionError):
            result = _filter_alive([12345])
        assert 12345 in result

    def test_empty_list(self) -> None:
        from bernstein.core.rolling_restart import _filter_alive

        assert _filter_alive([]) == []
