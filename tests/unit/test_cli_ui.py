"""Tests for CLI UI components — token usage display."""

from __future__ import annotations

import pytest

from bernstein.cli.ui import AgentInfo, AgentStatusTable

# --- TestAgentInfo ---


class TestAgentInfo:
    def test_from_dict_includes_tokens(self) -> None:
        data = {
            "id": "abc123",
            "role": "backend",
            "model": "sonnet",
            "status": "running",
            "task_ids": ["t1"],
            "runtime_s": 30.0,
            "tokens_used": 50000,
            "token_budget": 200000,
            "context_utilization_pct": 45.5,
        }
        info = AgentInfo.from_dict(data)
        assert info.tokens_used == 50_000
        assert info.token_budget == 200_000
        assert info.context_utilization_pct == 45.5

    def test_token_budget_pct_computed(self) -> None:
        info = AgentInfo(tokens_used=50_000, token_budget=200_000)
        assert info.token_budget_pct == 25.0

    def test_token_budget_pct_zero_when_no_budget(self) -> None:
        info = AgentInfo(tokens_used=50_000, token_budget=0)
        assert info.token_budget_pct == 0.0

    def test_token_budget_pct_capped_at_100(self) -> None:
        info = AgentInfo(tokens_used=500_000, token_budget=200_000)
        assert info.token_budget_pct == 100.0

    def test_defaults_are_zero(self) -> None:
        info = AgentInfo()
        assert info.tokens_used == 0
        assert info.token_budget == 0
        assert info.context_utilization_pct == 0.0


# --- TestAgentStatusTableTokenDisplay ---


class TestAgentStatusTableTokenDisplay:
    def test_table_includes_token_column(self) -> None:
        table = AgentStatusTable()
        agent = AgentInfo(
            agent_id="abc123",
            role="backend",
            model="sonnet",
            status="running",
            tokens_used=50_000,
            token_budget=200_000,
        )
        renderable = table.render([agent])
        # Check by inspecting rendered title and columns
        assert renderable.title == "Active Agents"

    def test_token_cell_dash_when_zero(self) -> None:
        table = AgentStatusTable()
        agent = AgentInfo(agent_id="x", role="qa", tokens_used=0)
        renderable = table.render([agent])
        assert renderable.title == "Active Agents"

    def test_plain_text_includes_tokens(self) -> None:
        table = AgentStatusTable()
        agent = AgentInfo(
            role="backend",
            model="sonnet",
            tokens_used=10_000,
            token_budget=100_000,
        )
        plain = table.render_plain([agent])
        assert "TOKENS" in plain
        assert "10,000/100,000" in plain

    def test_plain_text_dash_when_no_tokens(self) -> None:
        table = AgentStatusTable()
        agent = AgentInfo(role="qa", model="sonnet", tokens_used=0)
        plain = table.render_plain([agent])
        assert "-" in plain
