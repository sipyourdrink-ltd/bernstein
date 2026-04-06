"""Tests for TUI-005: Visual distinction for agent states."""

from __future__ import annotations

import time

from bernstein.tui.agent_states import (
    AGENT_STATE_COLORS,
    AGENT_STATE_INDICATORS,
    AGENT_STATE_LABELS,
    AgentState,
    AgentStateThresholds,
    agent_state_color,
    agent_state_indicator,
    agent_state_label,
    classify_agent_state,
    classify_from_api,
    render_agent_state,
    render_agent_state_compact,
)


class TestAgentStateEnum:
    def test_all_states_have_colors(self) -> None:
        for state in AgentState:
            assert state in AGENT_STATE_COLORS

    def test_all_states_have_indicators(self) -> None:
        for state in AgentState:
            assert state in AGENT_STATE_INDICATORS

    def test_all_states_have_labels(self) -> None:
        for state in AgentState:
            assert state in AGENT_STATE_LABELS


class TestClassifyAgentState:
    def test_done_status_is_dead(self) -> None:
        result = classify_agent_state(status="done", pid=123)
        assert result == AgentState.DEAD

    def test_failed_status_is_dead(self) -> None:
        result = classify_agent_state(status="failed")
        assert result == AgentState.DEAD

    def test_cancelled_status_is_dead(self) -> None:
        result = classify_agent_state(status="cancelled")
        assert result == AgentState.DEAD

    def test_no_pid_claimed_is_spawning(self) -> None:
        result = classify_agent_state(pid=None, status="claimed")
        assert result == AgentState.SPAWNING

    def test_no_pid_open_is_spawning(self) -> None:
        result = classify_agent_state(pid=None, status="open")
        assert result == AgentState.SPAWNING

    def test_no_pid_unknown_status_is_dead(self) -> None:
        result = classify_agent_state(pid=None, status="unknown")
        assert result == AgentState.DEAD

    def test_has_pid_in_progress_is_running(self) -> None:
        result = classify_agent_state(pid=1234, status="in_progress")
        assert result == AgentState.RUNNING

    def test_has_pid_claimed_is_spawning(self) -> None:
        result = classify_agent_state(pid=1234, status="claimed")
        assert result == AgentState.SPAWNING

    def test_stalled_by_heartbeat(self) -> None:
        now = time.time()
        old_heartbeat = now - 600  # 10 minutes ago
        result = classify_agent_state(
            pid=1234,
            status="in_progress",
            last_heartbeat=old_heartbeat,
            now=now,
        )
        assert result == AgentState.STALLED

    def test_not_stalled_with_recent_heartbeat(self) -> None:
        now = time.time()
        recent_heartbeat = now - 30  # 30 seconds ago
        result = classify_agent_state(
            pid=1234,
            status="in_progress",
            last_heartbeat=recent_heartbeat,
            now=now,
        )
        assert result == AgentState.RUNNING

    def test_spawn_timeout_is_dead(self) -> None:
        now = time.time()
        started_long_ago = now - 120  # 2 minutes ago
        result = classify_agent_state(
            pid=1234,
            status="spawning",
            started_at=started_long_ago,
            now=now,
        )
        assert result == AgentState.DEAD

    def test_custom_thresholds(self) -> None:
        now = time.time()
        thresholds = AgentStateThresholds(stall_threshold_s=10.0)
        old_heartbeat = now - 15
        result = classify_agent_state(
            pid=1234,
            status="in_progress",
            last_heartbeat=old_heartbeat,
            now=now,
            thresholds=thresholds,
        )
        assert result == AgentState.STALLED


class TestAgentStateHelpers:
    def test_color_for_running(self) -> None:
        assert agent_state_color(AgentState.RUNNING) == "green"

    def test_color_for_spawning(self) -> None:
        assert agent_state_color(AgentState.SPAWNING) == "yellow"

    def test_color_for_dead(self) -> None:
        assert agent_state_color(AgentState.DEAD) == "red"

    def test_indicator_is_single_char(self) -> None:
        for state in AgentState:
            indicator = agent_state_indicator(state)
            assert len(indicator) == 1

    def test_label_is_uppercase(self) -> None:
        for state in AgentState:
            label = agent_state_label(state)
            assert label == label.upper()


class TestRenderAgentState:
    def test_render_running(self) -> None:
        text = render_agent_state(AgentState.RUNNING)
        assert "running" in text.plain

    def test_render_accessible(self) -> None:
        text = render_agent_state(AgentState.RUNNING, accessible=True)
        assert "RUNNING" in text.plain

    def test_render_compact(self) -> None:
        text = render_agent_state_compact(AgentState.RUNNING)
        assert len(text.plain) == 1


class TestClassifyFromApi:
    def test_api_running(self) -> None:
        raw = {"pid": 1234, "status": "in_progress"}
        assert classify_from_api(raw) == AgentState.RUNNING

    def test_api_no_pid(self) -> None:
        raw = {"status": "claimed"}
        assert classify_from_api(raw) == AgentState.SPAWNING

    def test_api_done(self) -> None:
        raw = {"pid": 1234, "status": "done"}
        assert classify_from_api(raw) == AgentState.DEAD

    def test_api_with_heartbeat(self) -> None:
        now = time.time()
        raw = {
            "pid": 1234,
            "status": "in_progress",
            "last_heartbeat": now - 600,
        }
        assert classify_from_api(raw, now=now) == AgentState.STALLED
