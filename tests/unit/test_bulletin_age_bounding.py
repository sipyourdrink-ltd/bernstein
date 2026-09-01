"""Unit tests for bulletin board age bounding, stepwise weighting, and per-author cap."""

from __future__ import annotations

from bernstein.core.communication.bulletin import (
    DEFAULT_MESSAGE_TYPE_WEIGHTS,
    BulletinBoard,
    BulletinMessage,
)


def test_empty_board_summary() -> None:
    """Empty board produces empty summary string."""
    board = BulletinBoard()
    assert board.summary() == ""


def test_limit_zero_or_negative() -> None:
    """Limit <= 0 returns empty summary."""
    board = BulletinBoard()
    board.post(BulletinMessage(agent_id="agent-1", type="status", content="doing work"))
    assert board.summary(limit=0) == ""
    assert board.summary(limit=-1) == ""


def test_horizon_exclusion() -> None:
    """Messages beyond the horizon in sequence distance are excluded."""
    board = BulletinBoard()
    for i in range(10):
        board.post(BulletinMessage(agent_id=f"agent-{i}", type="status", content=f"step {i}"))

    # Total 10 messages (0..9). Horizon=4 means ages 0, 1, 2, 3 (messages 9, 8, 7, 6)
    summary = board.summary(horizon=4, limit=10)
    assert "step 9" in summary
    assert "step 8" in summary
    assert "step 7" in summary
    assert "step 6" in summary
    assert "step 5" not in summary
    assert "step 0" not in summary


def test_horizon_zero_or_negative() -> None:
    """Horizon <= 0 results in empty summary."""
    board = BulletinBoard()
    board.post(BulletinMessage(agent_id="agent-1", type="status", content="msg"))
    assert board.summary(horizon=0) == ""
    assert board.summary(horizon=-5) == ""


def test_horizon_with_explicit_sequence() -> None:
    """Horizon relative to an explicit current_sequence / current_tick."""
    board = BulletinBoard()
    # 5 messages posted (indices 0..4)
    for i in range(5):
        board.post(BulletinMessage(agent_id=f"agent-{i}", type="status", content=f"msg {i}"))

    # If current_sequence is 10 (5 ticks have passed since last post)
    # Message 4 has age = 10 - 1 - 4 = 5.
    # With horizon=5, age 5 is excluded.
    assert board.summary(horizon=5, current_sequence=10) == ""

    # With horizon=6, age 5 is included (msg 4 included, msg 3 with age 6 excluded)
    summary = board.summary(horizon=6, current_sequence=10)
    assert "msg 4" in summary
    assert "msg 3" not in summary


def test_stepwise_weighting_age_decay() -> None:
    """Messages in older step buckets are downweighted relative to newer step buckets."""
    board = BulletinBoard()
    # Post 10 status messages
    for i in range(10):
        board.post(BulletinMessage(agent_id=f"agent-{i}", type="status", content=f"status {i}"))

    # Step size 5:
    # Messages 9..5 are age 0..4 (step 0, weight 1.0)
    # Messages 4..0 are age 5..9 (step 1, weight 0.9 with default step_decay=0.1)
    summary = board.summary(step_size=5, limit=6)
    lines = summary.splitlines()
    assert len(lines) == 6
    # Top 5 should be step 0 (9, 8, 7, 6, 5), 6th should be msg 4 from step 1
    assert "status 9" in lines[0]
    assert "status 8" in lines[1]
    assert "status 7" in lines[2]
    assert "status 6" in lines[3]
    assert "status 5" in lines[4]
    assert "status 4" in lines[5]


def test_stepwise_weighting_high_priority_outranks_recent_status() -> None:
    """Older high-priority message (e.g. blocker) outranks newer low-priority status."""
    board = BulletinBoard()
    # Blocker posted earlier (age 5, step 1 with step_size=5)
    board.post(BulletinMessage(agent_id="agent-blocker", type="blocker", content="database locked"))
    # Followed by 5 status messages (age 4..0, step 0)
    for i in range(5):
        board.post(BulletinMessage(agent_id=f"agent-worker-{i}", type="status", content=f"ping {i}"))

    # Default weights: blocker=5.0, status=1.0.
    # At step 1 (age 5): blocker weight = 5.0 - 0.1 = 4.9.
    # At step 0 (age 0): status weight = 1.0.
    # Blocker (4.9) should appear at top of summary.
    summary = board.summary(limit=3, step_size=5)
    lines = summary.splitlines()
    assert len(lines) == 3
    assert "agent-blocker: database locked" in lines[0]


def test_custom_type_weights_and_decay() -> None:
    """Custom type weights and step decay parameters are respected."""
    board = BulletinBoard()
    board.post(BulletinMessage(agent_id="agent-finding", type="finding", content="key finding"))
    board.post(BulletinMessage(agent_id="agent-alert", type="alert", content="low alert"))

    # With custom type weights where finding is 10.0 and alert is 1.0:
    summary = board.summary(
        type_weights={"finding": 10.0, "alert": 1.0},
        step_decay=0.5,
        limit=2,
    )
    lines = summary.splitlines()
    assert "agent-finding: key finding" in lines[0]
    assert "agent-alert: low alert" in lines[1]


def test_per_author_cap() -> None:
    """Bursting agent is capped, preventing starvation of other agents."""
    board = BulletinBoard()
    # Agent A bursts 10 messages
    for i in range(10):
        board.post(BulletinMessage(agent_id="agent-burst", type="status", content=f"burst {i}"))
    # Agent B and C posted earlier messages
    board.post(BulletinMessage(agent_id="agent-quiet-b", type="status", content="important b"))
    board.post(BulletinMessage(agent_id="agent-quiet-c", type="status", content="important c"))

    # With max_per_author=2 and limit=4:
    # agent-quiet-c (age 0), agent-quiet-b (age 1), agent-burst top 2 (burst 9, burst 8)
    summary = board.summary(max_per_author=2, limit=4)
    assert "important c" in summary
    assert "important b" in summary
    assert "burst 9" in summary
    assert "burst 8" in summary
    assert "burst 7" not in summary


def test_per_author_cap_alias() -> None:
    """per_author_cap acts as alias to max_per_author."""
    board = BulletinBoard()
    for i in range(5):
        board.post(BulletinMessage(agent_id="agent-solo", type="status", content=f"item {i}"))
    summary = board.summary(per_author_cap=2, limit=5)
    lines = summary.splitlines()
    assert len(lines) == 2
    assert "item 4" in lines[0]
    assert "item 3" in lines[1]


def test_deterministic_byte_identical_output() -> None:
    """Identical board state yields byte-identical summary output."""
    board = BulletinBoard()
    board.post(BulletinMessage(agent_id="agent-1", type="status", content="init", timestamp=100.0))
    board.post(BulletinMessage(agent_id="agent-2", type="finding", content="found bug", timestamp=101.0))
    board.post(BulletinMessage(agent_id="agent-3", type="blocker", content="build failed", timestamp=102.0))
    board.post(BulletinMessage(agent_id="agent-1", type="alert", content="high cpu", timestamp=103.0))

    summary1 = board.summary(limit=4, horizon=10, max_per_author=2)
    summary2 = board.summary(limit=4, horizon=10, max_per_author=2)
    assert summary1 == summary2
    assert isinstance(summary1, str)
    assert len(summary1) > 0


def test_default_type_weights_mapping() -> None:
    """DEFAULT_MESSAGE_TYPE_WEIGHTS defines expected priority hierarchy."""
    assert DEFAULT_MESSAGE_TYPE_WEIGHTS["blocker"] > DEFAULT_MESSAGE_TYPE_WEIGHTS["alert"]
    assert DEFAULT_MESSAGE_TYPE_WEIGHTS["alert"] > DEFAULT_MESSAGE_TYPE_WEIGHTS["finding"]
    assert DEFAULT_MESSAGE_TYPE_WEIGHTS["finding"] > DEFAULT_MESSAGE_TYPE_WEIGHTS["dependency"]
    assert DEFAULT_MESSAGE_TYPE_WEIGHTS["dependency"] > DEFAULT_MESSAGE_TYPE_WEIGHTS["status"]
