"""Tests for AgentActivitySummary and BulletinBoard activity summary methods."""

from __future__ import annotations

import time

from bernstein.core.bulletin import AgentActivitySummary, BulletinBoard


class TestAgentActivitySummary:
    def test_to_dict_roundtrip(self) -> None:
        s = AgentActivitySummary(agent_id="agent-1", summary="coding in progress", timestamp=1700000000.0)
        d = s.to_dict()
        assert d == {"agent_id": "agent-1", "summary": "coding in progress", "timestamp": 1700000000.0}
        restored = AgentActivitySummary.from_dict(d)
        assert restored.agent_id == s.agent_id
        assert restored.summary == s.summary
        assert restored.timestamp == s.timestamp

    def test_default_timestamp_is_recent(self) -> None:
        before = time.time()
        s = AgentActivitySummary(agent_id="a", summary="idle no recent activity")
        after = time.time()
        assert before <= s.timestamp <= after


class TestBulletinBoardActivitySummaries:
    def test_post_and_retrieve_summary(self) -> None:
        board = BulletinBoard()
        s = AgentActivitySummary(agent_id="agent-1", summary="coding in progress")
        board.post_activity_summary(s)
        result = board.get_latest_activity_summary("agent-1")
        assert result is not None
        assert result.agent_id == "agent-1"
        assert result.summary == "coding in progress"

    def test_returns_none_for_unknown_agent(self) -> None:
        board = BulletinBoard()
        assert board.get_latest_activity_summary("nonexistent") is None

    def test_latest_summary_overwrites_previous(self) -> None:
        board = BulletinBoard()
        board.post_activity_summary(AgentActivitySummary(agent_id="a1", summary="planning in progress"))
        board.post_activity_summary(AgentActivitySummary(agent_id="a1", summary="coding in progress"))
        result = board.get_latest_activity_summary("a1")
        assert result is not None
        assert result.summary == "coding in progress"

    def test_get_all_summaries_multiple_agents(self) -> None:
        board = BulletinBoard()
        board.post_activity_summary(AgentActivitySummary(agent_id="a1", summary="coding in progress"))
        board.post_activity_summary(AgentActivitySummary(agent_id="a2", summary="testing task completed"))
        all_summaries = board.get_all_activity_summaries()
        assert set(all_summaries.keys()) == {"a1", "a2"}
        assert all_summaries["a1"].summary == "coding in progress"
        assert all_summaries["a2"].summary == "testing task completed"

    def test_get_all_summaries_returns_copy(self) -> None:
        board = BulletinBoard()
        board.post_activity_summary(AgentActivitySummary(agent_id="a1", summary="coding in progress"))
        summaries = board.get_all_activity_summaries()
        summaries["a1"] = AgentActivitySummary(agent_id="a1", summary="mutated")
        # Original board should be unaffected
        assert board.get_latest_activity_summary("a1").summary == "coding in progress"  # type: ignore[union-attr]

    def test_summary_includes_timestamp(self) -> None:
        board = BulletinBoard()
        before = time.time()
        board.post_activity_summary(AgentActivitySummary(agent_id="a1", summary="idle no recent activity"))
        after = time.time()
        result = board.get_latest_activity_summary("a1")
        assert result is not None
        assert before <= result.timestamp <= after

    def test_empty_board_returns_empty_dict(self) -> None:
        board = BulletinBoard()
        assert board.get_all_activity_summaries() == {}
