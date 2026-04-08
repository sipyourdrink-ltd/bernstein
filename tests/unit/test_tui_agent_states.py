"""Tests for TUI-005: Visual distinction for agent states."""

from __future__ import annotations

import time

from bernstein.tui.agent_states import (
    AGENT_STATE_COLORS,
    AGENT_STATE_INDICATORS,
    AGENT_STATE_LABELS,
    AGENT_STATE_SPINNER_FRAMES,
    ANIMATED_STATES,
    AgentState,
    AgentStateThresholds,
    agent_state_color,
    agent_state_indicator,
    agent_state_label,
    classify_agent_state,
    classify_from_api,
    get_spinner_frame,
    render_agent_state,
    render_agent_state_animated,
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


class TestMergingAndIdleStates:
    def test_merging_status_is_merging(self) -> None:
        result = classify_agent_state(pid=1234, status="merging")
        assert result == AgentState.MERGING

    def test_committing_status_is_merging(self) -> None:
        result = classify_agent_state(pid=1234, status="committing")
        assert result == AgentState.MERGING

    def test_pushing_status_is_merging(self) -> None:
        result = classify_agent_state(pid=1234, status="pushing")
        assert result == AgentState.MERGING

    def test_idle_status_is_idle(self) -> None:
        result = classify_agent_state(status="idle")
        assert result == AgentState.IDLE

    def test_waiting_status_is_idle(self) -> None:
        result = classify_agent_state(status="waiting")
        assert result == AgentState.IDLE

    def test_paused_status_is_idle(self) -> None:
        result = classify_agent_state(status="paused")
        assert result == AgentState.IDLE

    def test_merging_color_is_blue(self) -> None:
        assert agent_state_color(AgentState.MERGING) == "blue"

    def test_idle_color_is_gray(self) -> None:
        assert agent_state_color(AgentState.IDLE) == "bright_black"

    def test_api_merging(self) -> None:
        raw = {"pid": 1234, "status": "merging"}
        assert classify_from_api(raw) == AgentState.MERGING


class TestSpinnerFrames:
    def test_animated_states_have_frames(self) -> None:
        for state in ANIMATED_STATES:
            assert state in AGENT_STATE_SPINNER_FRAMES
            assert len(AGENT_STATE_SPINNER_FRAMES[state]) > 0

    def test_spawning_is_animated(self) -> None:
        assert AgentState.SPAWNING in ANIMATED_STATES

    def test_running_is_animated(self) -> None:
        assert AgentState.RUNNING in ANIMATED_STATES

    def test_merging_is_animated(self) -> None:
        assert AgentState.MERGING in ANIMATED_STATES

    def test_dead_is_not_animated(self) -> None:
        assert AgentState.DEAD not in ANIMATED_STATES

    def test_spinner_cycles_frames(self) -> None:
        frames = AGENT_STATE_SPINNER_FRAMES[AgentState.SPAWNING]
        seen = {get_spinner_frame(AgentState.SPAWNING, i) for i in range(len(frames))}
        assert len(seen) == len(frames)

    def test_spinner_fallback_for_static_state(self) -> None:
        frame = get_spinner_frame(AgentState.DEAD, tick=0)
        assert frame == agent_state_indicator(AgentState.DEAD)

    def test_spinner_frame_is_single_char(self) -> None:
        for state in ANIMATED_STATES:
            for tick in range(8):
                frame = get_spinner_frame(state, tick)
                assert len(frame) == 1

    def test_render_animated_running(self) -> None:
        text = render_agent_state_animated(AgentState.RUNNING, tick=0)
        assert "running" in text.plain

    def test_render_animated_merging(self) -> None:
        text = render_agent_state_animated(AgentState.MERGING, tick=3)
        assert "merging" in text.plain

    def test_render_animated_differs_across_ticks(self) -> None:
        frames = AGENT_STATE_SPINNER_FRAMES[AgentState.SPAWNING]
        if len(frames) > 1:
            t0 = render_agent_state_animated(AgentState.SPAWNING, tick=0).plain
            t1 = render_agent_state_animated(AgentState.SPAWNING, tick=1).plain
            assert t0 != t1
