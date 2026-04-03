"""Tests for bernstein.core.traces — TraceStep, parse_agent_log, replay payload."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from bernstein.core.traces import (
    AgentTrace,
    TraceStep,
    TraceStore,
    new_trace,
    parse_agent_log,
    parse_log_to_steps,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# TraceStep serialization
# ---------------------------------------------------------------------------


class TestTraceStepSerialization:
    def test_round_trip_minimal(self) -> None:
        step = TraceStep(type="orient", timestamp=1_000_000.0)
        restored = TraceStep.from_dict(step.to_dict())
        assert restored.type == "orient"
        assert abs(restored.timestamp - 1_000_000.0) < 1e-6
        assert restored.detail == ""
        assert restored.files == []
        assert restored.tokens == 0
        assert restored.duration_ms == 0

    def test_round_trip_full(self) -> None:
        step = TraceStep(
            type="edit",
            timestamp=1_234_567.89,
            detail="Updated foo.py",
            files=["src/foo.py", "src/bar.py"],
            tokens=512,
            duration_ms=1200,
        )
        d = step.to_dict()
        restored = TraceStep.from_dict(d)
        assert restored.type == "edit"
        assert restored.detail == "Updated foo.py"
        assert restored.files == ["src/foo.py", "src/bar.py"]
        assert restored.tokens == 512
        assert restored.duration_ms == 1200

    def test_to_dict_returns_plain_dict(self) -> None:
        step = TraceStep(type="verify", timestamp=0.0, detail="ran tests")
        d = step.to_dict()
        assert isinstance(d, dict)
        assert d["type"] == "verify"

    def test_from_dict_with_missing_optional_fields(self) -> None:
        # Only required fields present
        d: dict[str, object] = {"type": "plan", "timestamp": 42.0}
        step = TraceStep.from_dict(d)
        assert step.type == "plan"
        assert step.tokens == 0
        assert step.files == []

    def test_asdict_matches_to_dict(self) -> None:
        step = TraceStep(type="spawn", timestamp=1.0, detail="spawned")
        assert asdict(step) == step.to_dict()


# ---------------------------------------------------------------------------
# parse_agent_log / parse_log_to_steps
# ---------------------------------------------------------------------------


class TestParseAgentLog:
    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path) -> None:
        result = parse_agent_log(tmp_path / "missing.log")
        assert result == []

    def test_returns_empty_for_empty_file(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("")
        result = parse_agent_log(log)
        assert result == []

    def test_extracts_orient_step_from_read_tool(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Read] /src/bernstein/core/traces.py\n")
        steps = parse_agent_log(log)
        assert any(s.type == "orient" for s in steps)

    def test_extracts_edit_step_from_edit_tool(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Edit] /src/bernstein/core/traces.py\n")
        steps = parse_agent_log(log)
        assert any(s.type == "edit" for s in steps)

    def test_extracts_verify_step_from_bash_tool(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Bash] uv run pytest tests/ -x\n")
        steps = parse_agent_log(log)
        assert any(s.type == "verify" for s in steps)

    def test_extracts_multiple_step_types(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text(
            "[Read] /src/foo.py\n[Glob] **/*.py\nSome planning text here.\n[Edit] /src/bar.py\n[Bash] uv run pytest\n"
        )
        steps = parse_agent_log(log)
        types = {s.type for s in steps}
        # orient from Read+Glob, edit from Edit, verify from Bash, plan from text
        assert "orient" in types
        assert "edit" in types
        assert "verify" in types

    def test_files_are_populated(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Read] /src/bernstein/core/traces.py\n")
        steps = parse_agent_log(log)
        orient_steps = [s for s in steps if s.type == "orient"]
        assert orient_steps
        all_files = [f for s in orient_steps for f in s.files]
        assert any("traces.py" in f for f in all_files)

    def test_parse_agent_log_is_alias_for_parse_log_to_steps(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Read] /src/foo.py\n[Edit] /src/bar.py\n")
        assert parse_agent_log(log) == parse_log_to_steps(log)

    def test_collapses_consecutive_same_type(self, tmp_path: Path) -> None:
        """Multiple consecutive Read lines should produce ONE orient step."""
        log = tmp_path / "agent.log"
        log.write_text("[Read] /src/a.py\n[Read] /src/b.py\n[Read] /src/c.py\n")
        steps = parse_agent_log(log)
        orient_steps = [s for s in steps if s.type == "orient"]
        assert len(orient_steps) == 1
        assert len(orient_steps[0].files) == 3

    def test_glob_tool_maps_to_orient(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Glob] **/*.py\n")
        steps = parse_agent_log(log)
        assert any(s.type == "orient" for s in steps)

    def test_grep_tool_maps_to_orient(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("[Grep] pattern\n")
        steps = parse_agent_log(log)
        assert any(s.type == "orient" for s in steps)


# ---------------------------------------------------------------------------
# TraceStore / AgentTrace
# ---------------------------------------------------------------------------


class TestTraceStore:
    def test_write_and_read_by_trace_id(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "traces")
        trace = new_trace("sess-1", ["task-abc"], "backend", "sonnet", "high")
        store.write(trace)

        loaded = store.read_by_trace_id(trace.trace_id)
        assert loaded is not None
        assert loaded.trace_id == trace.trace_id
        assert loaded.session_id == "sess-1"

    def test_write_and_read_by_task_id(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "traces")
        trace = new_trace("sess-2", ["task-xyz"], "qa", "haiku", "medium")
        store.write(trace)

        loaded_list = store.read_by_task("task-xyz")
        assert len(loaded_list) == 1
        assert loaded_list[0].trace_id == trace.trace_id

    def test_list_traces_returns_recent_first(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "traces")
        t1 = new_trace("sess-a", ["task-1"], "backend", "sonnet", "high")
        t2 = new_trace("sess-b", ["task-2"], "qa", "sonnet", "high")
        # Ensure t2 has a later spawn_ts
        t2.spawn_ts = t1.spawn_ts + 10
        store.write(t1)
        store.write(t2)

        listed = store.list_traces()
        assert listed[0].trace_id == t2.trace_id
        assert listed[1].trace_id == t1.trace_id

    def test_read_missing_task_returns_empty(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "traces")
        assert store.read_by_task("nonexistent") == []

    def test_read_missing_trace_id_returns_none(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "traces")
        assert store.read_by_trace_id("deadbeef") is None

    def test_multiple_traces_for_same_task(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "traces")
        t1 = new_trace("sess-1", ["shared-task"], "backend", "sonnet", "high")
        t2 = new_trace("sess-2", ["shared-task"], "backend", "opus", "max")
        store.write(t1)
        store.write(t2)

        loaded = store.read_by_task("shared-task")
        assert len(loaded) == 2
        trace_ids = {t.trace_id for t in loaded}
        assert t1.trace_id in trace_ids
        assert t2.trace_id in trace_ids


# ---------------------------------------------------------------------------
# Replay payload construction
# ---------------------------------------------------------------------------


class TestReplayPayloadConstruction:
    """Verify that replay constructs the correct task payload from a trace."""

    def _make_trace_with_snapshot(self) -> AgentTrace:
        trace = new_trace(
            session_id="sess-replay",
            task_ids=["orig-task-1"],
            role="backend",
            model="sonnet",
            effort="high",
        )
        trace.task_snapshots = [
            {
                "id": "orig-task-1",
                "title": "Fix the authentication bug",
                "description": "Users cannot log in with OAuth tokens.",
                "role": "backend",
                "priority": 2,
                "scope": "medium",
                "complexity": "medium",
            }
        ]
        return trace

    def test_payload_title_prefixed_with_replay(self) -> None:
        trace = self._make_trace_with_snapshot()
        snapshot = trace.task_snapshots[0]
        payload = {
            "title": f"[replay] {snapshot.get('title', 'orig-task-1')}",
            "description": snapshot.get("description", ""),
            "role": snapshot.get("role", trace.agent_role),
            "priority": snapshot.get("priority", 2),
            "scope": snapshot.get("scope", "medium"),
            "complexity": snapshot.get("complexity", "medium"),
            "model": trace.model,
            "effort": trace.effort,
        }
        assert payload["title"] == "[replay] Fix the authentication bug"

    def test_payload_preserves_description(self) -> None:
        trace = self._make_trace_with_snapshot()
        snapshot = trace.task_snapshots[0]
        payload = {
            "title": f"[replay] {snapshot.get('title', '')}",
            "description": snapshot.get("description", ""),
            "role": snapshot.get("role", trace.agent_role),
            "priority": snapshot.get("priority", 2),
            "scope": snapshot.get("scope", "medium"),
            "complexity": snapshot.get("complexity", "medium"),
            "model": trace.model,
            "effort": trace.effort,
        }
        assert "OAuth tokens" in payload["description"]

    def test_payload_overrides_model(self) -> None:
        trace = self._make_trace_with_snapshot()
        snapshot = trace.task_snapshots[0]
        override_model = "opus"
        effective_model = override_model or trace.model
        payload = {
            "title": f"[replay] {snapshot.get('title', '')}",
            "description": snapshot.get("description", ""),
            "role": snapshot.get("role", trace.agent_role),
            "priority": snapshot.get("priority", 2),
            "scope": snapshot.get("scope", "medium"),
            "complexity": snapshot.get("complexity", "medium"),
            "model": effective_model,
            "effort": trace.effort,
        }
        assert payload["model"] == "opus"

    def test_payload_overrides_effort(self) -> None:
        trace = self._make_trace_with_snapshot()
        snapshot = trace.task_snapshots[0]
        override_effort = "max"
        effective_effort = override_effort or trace.effort
        payload = {
            "title": f"[replay] {snapshot.get('title', '')}",
            "description": "",
            "role": snapshot.get("role", trace.agent_role),
            "priority": snapshot.get("priority", 2),
            "scope": snapshot.get("scope", "medium"),
            "complexity": snapshot.get("complexity", "medium"),
            "model": trace.model,
            "effort": effective_effort,
        }
        assert payload["effort"] == "max"

    def test_payload_falls_back_to_trace_model_when_no_override(self) -> None:
        trace = self._make_trace_with_snapshot()
        snapshot = trace.task_snapshots[0]
        override_model: str | None = None
        effective_model = override_model or trace.model
        payload: dict[str, object] = {
            "title": f"[replay] {snapshot.get('title', '')}",
            "description": "",
            "role": snapshot.get("role", trace.agent_role),
            "priority": snapshot.get("priority", 2),
            "scope": snapshot.get("scope", "medium"),
            "complexity": snapshot.get("complexity", "medium"),
            "model": effective_model,
            "effort": trace.effort,
        }
        assert payload["model"] == "sonnet"

    def test_snapshot_lookup_by_task_id(self) -> None:
        trace = self._make_trace_with_snapshot()
        task_id = "orig-task-1"
        snapshot = next(
            (s for s in trace.task_snapshots if s.get("id") == task_id),
            None,
        )
        assert snapshot is not None
        assert snapshot["id"] == task_id

    def test_snapshot_lookup_returns_none_for_unknown_id(self) -> None:
        trace = self._make_trace_with_snapshot()
        snapshot = next(
            (s for s in trace.task_snapshots if s.get("id") == "unknown-id"),
            None,
        )
        assert snapshot is None


# ---------------------------------------------------------------------------
# new_trace / finalize_trace integration
# ---------------------------------------------------------------------------


class TestNewTrace:
    def test_spawn_step_is_created(self) -> None:
        trace = new_trace("sess-x", ["t1", "t2"], "qa", "haiku", "low")
        assert len(trace.steps) == 1
        assert trace.steps[0].type == "spawn"

    def test_spawn_step_detail_contains_role(self) -> None:
        trace = new_trace("sess-y", ["t1"], "security", "opus", "max")
        assert "security" in trace.steps[0].detail

    def test_trace_id_is_16_hex_chars(self) -> None:
        trace = new_trace("sess-z", ["t1"], "backend", "sonnet", "high")
        assert len(trace.trace_id) == 16
        assert all(c in "0123456789abcdef" for c in trace.trace_id)

    def test_end_ts_is_none_initially(self) -> None:
        trace = new_trace("sess-w", ["t1"], "backend", "sonnet", "high")
        assert trace.end_ts is None
        assert trace.duration_s is None

    def test_outcome_defaults_to_unknown(self) -> None:
        trace = new_trace("sess-v", ["t1"], "qa", "haiku", "medium")
        assert trace.outcome == "unknown"


from bernstein.core.traces import record_turn_budget


def test_trace_step_has_budget_fields() -> None:
    """TraceStep stores per-turn budget accounting fields."""
    step = TraceStep(
        type="orient",
        timestamp=1234567890.0,
        turn_number=2,
        allocated_budget=20000,
        consumed_this_turn=12000,
        remaining_budget=8000,
    )
    assert step.turn_number == 2
    assert step.remaining_budget == 8000

    data = step.to_dict()
    assert data["allocated_budget"] == 20000

    restored = TraceStep.from_dict(data)
    assert restored.consumed_this_turn == 12000


def test_agent_trace_has_turn_budget_totals() -> None:
    """AgentTrace stores aggregated turn budget fields."""
    trace = AgentTrace(
        trace_id="abc123",
        session_id="sess-1",
        task_ids=["t1"],
        agent_role="backend",
        model="sonnet",
        effort="normal",
        spawn_ts=0.0,
        total_allocated_budget=40000,
        total_consumed=24000,
        turn_count=3,
    )
    assert trace.turn_count == 3
    assert trace.total_allocated_budget == 40000


def test_record_turn_budget_updates_trace() -> None:
    """record_turn_budget creates a step and mutates the trace totals."""
    trace = AgentTrace(
        trace_id="xyz",
        session_id="s-1",
        task_ids=["t1"],
        agent_role="backend",
        model="sonnet",
        effort="normal",
        spawn_ts=0.0,
    )
    step = record_turn_budget(trace, turn_number=1, allocated=20000, consumed=5000, remaining=15000)
    trace.steps.append(step)

    assert trace.turn_count == 1
    assert trace.total_consumed == 5000
    assert trace.total_allocated_budget == 20000
    assert step.consumed_this_turn == 5000
    assert "turn 1" in step.detail

    # Second turn accumulates
    record_turn_budget(trace, turn_number=2, allocated=25000, consumed=8000, remaining=17000)
    assert trace.turn_count == 2
    assert trace.total_consumed == 13000  # 5000 + 8000


def test_record_compaction_boundary_creates_compact_step() -> None:
    """record_compaction_boundary creates a step with type='compact'."""
    from bernstein.core.traces import record_compaction_boundary

    trace = AgentTrace(
        trace_id="t1",
        session_id="s-1",
        task_ids=["task-1"],
        agent_role="backend",
        model="sonnet",
        effort="normal",
        spawn_ts=0.0,
    )
    step = record_compaction_boundary(
        trace,
        correlation_id="comp-001",
        tokens_before=18000,
        tokens_after=5000,
        reason="token_budget",
    )
    trace.steps.append(step)

    assert step.type == "compact"
    assert step.compaction_correlation_id == "comp-001"
    assert step.compaction_tokens_before == 18000
    assert step.compaction_tokens_after == 5000
    assert step.compaction_reason == "token_budget"
    assert step.tokens == 13000  # saved tokens
    assert "compaction v1" in step.detail
    assert "18000" in step.detail
    assert "5000" in step.detail


def test_record_compaction_boundary_serialization_roundtrip() -> None:
    """Compaction fields survive to_dict / from_dict roundtrip."""
    from bernstein.core.traces import record_compaction_boundary

    trace = AgentTrace(
        trace_id="t2",
        session_id="s-2",
        task_ids=["task-2"],
        agent_role="qa",
        model="opus",
        effort="high",
        spawn_ts=0.0,
    )
    step = record_compaction_boundary(
        trace,
        correlation_id="comp-002",
        tokens_before=10000,
        tokens_after=9000,
        reason="reactive_fallback",
    )

    data = step.to_dict()
    assert data["type"] == "compact"
    assert data["compaction_correlation_id"] == "comp-002"
    assert data["compaction_tokens_before"] == 10000

    restored = TraceStep.from_dict(data)
    assert restored.type == "compact"
    assert restored.compaction_tokens_after == 9000
    assert restored.compaction_reason == "reactive_fallback"


def test_record_compaction_boundary_zero_savings() -> None:
    """Compaction with no token reduction still records cleanly."""
    from bernstein.core.traces import record_compaction_boundary

    trace = AgentTrace(
        trace_id="t3",
        session_id="s-3",
        task_ids=["task-3"],
        agent_role="backend",
        model="sonnet",
        effort="normal",
        spawn_ts=0.0,
    )
    step = record_compaction_boundary(
        trace,
        correlation_id="comp-003",
        tokens_before=5000,
        tokens_after=5000,
        reason="manual",
    )
    assert step.tokens == 0  # no savings
    assert step.compaction_tokens_before == 5000
    assert step.compaction_tokens_after == 5000
