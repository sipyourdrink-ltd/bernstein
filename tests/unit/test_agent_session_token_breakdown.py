"""Tests for agent session token consumption breakdown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.agent_session_token_breakdown import (
    AgentSessionTokenBreakdown,
    load_all_session_breakdowns,
    load_session_breakdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_prompt_report(sdd_dir: Path, session_id: str, **kwargs: object) -> None:
    """Write a fake prompt token usage report to the metrics dir."""
    metrics_dir = sdd_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "session_id": session_id,
        "total_tokens": 10_000,
        "system_prompt_tokens": 1_000,
        "context_tokens": 5_000,
        "user_prompt_tokens": 4_000,
        "system_prompt_pct": 10.0,
        "context_pct": 50.0,
        "user_prompt_pct": 40.0,
        "sections": [],
        "suggestions": [],
    }
    defaults.update(kwargs)
    (metrics_dir / f"prompt_token_usage_{session_id}.json").write_text(json.dumps(defaults), encoding="utf-8")


def _make_tracker_file(sdd_dir: Path, run_id: str, usages: list[dict]) -> None:
    costs_dir = sdd_dir / "runtime" / "costs"
    costs_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id": run_id,
        "budget_usd": 10.0,
        "spent_usd": sum(u.get("cost_usd", 0.0) for u in usages),
        "warn_threshold": 0.8,
        "critical_threshold": 0.95,
        "hard_stop_threshold": 1.0,
        "usages": usages,
        "cumulative_tokens": {},
    }
    (costs_dir / f"{run_id}.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# AgentSessionTokenBreakdown dataclass
# ---------------------------------------------------------------------------


class TestAgentSessionTokenBreakdown:
    def test_total_tokens(self) -> None:
        b = AgentSessionTokenBreakdown(
            session_id="s1",
            actual_input_tokens=8_000,
            output_tokens=2_000,
        )
        assert b.total_tokens == 10_000

    def test_percentages_sum_to_100(self) -> None:
        b = AgentSessionTokenBreakdown(
            session_id="s1",
            system_prompt_tokens=1_000,
            context_tokens=4_000,
            user_prompt_tokens=2_000,
            tool_result_tokens=1_000,
            output_tokens=2_000,
            actual_input_tokens=8_000,
        )
        pct = b.percentages()
        total = sum(pct.values())
        assert abs(total - 100.0) < 1.0, f"percentages sum to {total}"

    def test_percentages_zero_when_no_tokens(self) -> None:
        b = AgentSessionTokenBreakdown(session_id="empty")
        pct = b.percentages()
        assert all(v == pytest.approx(0.0) for v in pct.values())

    def test_to_dict_structure(self) -> None:
        b = AgentSessionTokenBreakdown(
            session_id="s1",
            system_prompt_tokens=1_000,
            context_tokens=5_000,
            user_prompt_tokens=2_000,
            tool_result_tokens=500,
            output_tokens=2_000,
            actual_input_tokens=8_500,
            model="sonnet",
            cost_usd=0.05,
            task_id="t1",
            optimization_notes=["trim context"],
        )
        d = b.to_dict()
        assert d["session_id"] == "s1"
        assert d["model"] == "sonnet"
        assert d["total_tokens"] == 10_500
        assert "breakdown" in d
        assert d["breakdown"]["system_prompt_tokens"] == 1_000
        assert d["breakdown"]["tool_result_tokens"] == 500
        assert "percentages" in d
        assert d["optimization_notes"] == ["trim context"]

    def test_summary_includes_session(self) -> None:
        b = AgentSessionTokenBreakdown(session_id="abc", model="opus")
        summary = b.summary()
        assert "abc" in summary
        assert "opus" in summary


# ---------------------------------------------------------------------------
# load_session_breakdown
# ---------------------------------------------------------------------------


class TestLoadSessionBreakdown:
    def test_combines_prompt_report_with_actual(self, tmp_path: Path) -> None:
        _write_prompt_report(
            tmp_path,
            "sess1",
            system_prompt_tokens=1_500,
            context_tokens=6_000,
            user_prompt_tokens=2_500,
        )
        b = load_session_breakdown(
            sdd_dir=tmp_path,
            session_id="sess1",
            actual_input_tokens=12_000,
            actual_output_tokens=3_000,
            model="sonnet",
            cost_usd=0.10,
        )
        assert b.system_prompt_tokens == 1_500
        assert b.context_tokens == 6_000
        assert b.user_prompt_tokens == 2_500
        # tool_result = 12_000 - (1_500 + 6_000 + 2_500) = 2_000
        assert b.tool_result_tokens == 2_000
        assert b.output_tokens == 3_000
        assert b.actual_input_tokens == 12_000

    def test_no_prompt_report_returns_input_output_only(self, tmp_path: Path) -> None:
        b = load_session_breakdown(
            sdd_dir=tmp_path,
            session_id="no_report",
            actual_input_tokens=8_000,
            actual_output_tokens=2_000,
        )
        assert b.system_prompt_tokens == 0
        assert b.context_tokens == 0
        assert b.user_prompt_tokens == 0
        assert b.tool_result_tokens == 8_000  # all input is "tool results" since no estimate
        assert len(b.optimization_notes) == 1
        assert "No pre-session prompt analysis" in b.optimization_notes[0]

    def test_tool_result_tokens_clamped_to_zero(self, tmp_path: Path) -> None:
        """Actual input less than estimate → tool_result_tokens == 0 (cache dominated)."""
        _write_prompt_report(
            tmp_path,
            "sess2",
            system_prompt_tokens=5_000,
            context_tokens=4_000,
            user_prompt_tokens=2_000,
        )
        b = load_session_breakdown(
            sdd_dir=tmp_path,
            session_id="sess2",
            actual_input_tokens=3_000,  # less than estimated 11_000
            actual_output_tokens=1_000,
        )
        assert b.tool_result_tokens == 0

    def test_high_context_note(self, tmp_path: Path) -> None:
        """Flag sessions where context > 55% of actual input."""
        _write_prompt_report(
            tmp_path,
            "ctx_heavy",
            system_prompt_tokens=500,
            context_tokens=8_000,  # 80% of 10k input
            user_prompt_tokens=1_500,
        )
        b = load_session_breakdown(
            sdd_dir=tmp_path,
            session_id="ctx_heavy",
            actual_input_tokens=10_000,
            actual_output_tokens=2_000,
        )
        note_text = " ".join(b.optimization_notes)
        assert "Context sections" in note_text

    def test_suggestions_from_prompt_report_carried_over(self, tmp_path: Path) -> None:
        _write_prompt_report(
            tmp_path,
            "over_budget",
            suggestions=["Context is 60% (recommended ≤50%). Trim: context."],
        )
        b = load_session_breakdown(sdd_dir=tmp_path, session_id="over_budget")
        assert any("recommended" in note for note in b.optimization_notes)


# ---------------------------------------------------------------------------
# load_all_session_breakdowns
# ---------------------------------------------------------------------------


class TestLoadAllSessionBreakdowns:
    def test_returns_empty_without_costs_dir(self, tmp_path: Path) -> None:
        results = load_all_session_breakdowns(tmp_path)
        assert results == []

    def test_loads_from_cost_files(self, tmp_path: Path) -> None:
        usages = [
            {
                "input_tokens": 5_000,
                "output_tokens": 1_000,
                "model": "sonnet",
                "cost_usd": 0.02,
                "agent_id": "agent-a",
                "task_id": "t1",
                "tenant_id": "default",
                "timestamp": 1000.0,
                "cache_hit": False,
                "cached_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
        ]
        _make_tracker_file(tmp_path, "run-1", usages)
        results = load_all_session_breakdowns(tmp_path)
        assert len(results) == 1
        b = results[0]
        assert b.session_id == "agent-a"
        assert b.actual_input_tokens == 5_000
        assert b.output_tokens == 1_000

    def test_deduplicates_by_agent_id(self, tmp_path: Path) -> None:
        """Same agent_id across multiple cost files → only first entry kept."""
        usage = {
            "input_tokens": 1_000,
            "output_tokens": 200,
            "model": "haiku",
            "cost_usd": 0.01,
            "agent_id": "agent-dup",
            "task_id": "t1",
            "tenant_id": "default",
            "timestamp": 1000.0,
            "cache_hit": False,
            "cached_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        # Write two files with the same agent
        costs_dir = tmp_path / "runtime" / "costs"
        costs_dir.mkdir(parents=True, exist_ok=True)
        import time

        _make_tracker_file(tmp_path, "run-a", [usage])
        time.sleep(0.01)
        _make_tracker_file(tmp_path, "run-b", [usage])
        results = load_all_session_breakdowns(tmp_path)
        assert len(results) == 1
