import time
from pathlib import Path

from bernstein.core.bulletin import (
    BulletinBoard,
    BulletinMessage,
    DelegationStatus,
    MessageBoard,
)


def test_bulletin_board_post_and_read_since() -> None:
    """Test posting messages and reading them back with time filters."""
    board = BulletinBoard()

    # Post message 1
    msg1 = board.post(BulletinMessage(agent_id="a1", type="status", content="msg1"))
    assert msg1.timestamp > 0

    time.sleep(0.01)
    ts_mid = time.time()
    time.sleep(0.01)

    # Post message 2
    msg2 = board.post(BulletinMessage(agent_id="a2", type="finding", content="msg2"))
    assert msg2.timestamp > ts_mid

    # Read all
    all_msgs = board.read_since(0)
    assert len(all_msgs) == 2
    assert all_msgs[0].content == "msg1"
    assert all_msgs[1].content == "msg2"

    # Read since mid
    since_mid = board.read_since(ts_mid)
    assert len(since_mid) == 1
    assert since_mid[0].content == "msg2"


def test_bulletin_board_read_by_type() -> None:
    """Test filtering messages by type."""
    board = BulletinBoard()
    board.post(BulletinMessage("a1", "status", "s1"))
    board.post(BulletinMessage("a2", "finding", "f1"))
    board.post(BulletinMessage("a3", "status", "s2"))

    status_msgs = board.read_by_type("status")
    assert len(status_msgs) == 2
    assert {m.content for m in status_msgs} == {"s1", "s2"}

    finding_msgs = board.read_by_type("finding")
    assert len(finding_msgs) == 1
    assert finding_msgs[0].content == "f1"


def test_bulletin_board_cell_isolation() -> None:
    """Test filtering messages by cell_id."""
    board = BulletinBoard()
    board.post(BulletinMessage("a1", "status", "c1", cell_id="cell-A"))
    board.post(BulletinMessage("a2", "status", "c2", cell_id="cell-B"))
    board.post(BulletinMessage("a3", "status", "global", cell_id=None))

    cell_a = board.read_by_cell("cell-A")
    assert len(cell_a) == 1
    assert cell_a[0].content == "c1"

    cell_b = board.read_by_cell("cell-B")
    assert len(cell_b) == 1
    assert cell_b[0].content == "c2"


def test_bulletin_board_persistence(tmp_path: Path) -> None:
    """Test flushing to and loading from disk."""
    board1 = BulletinBoard()
    board1.post(BulletinMessage("a1", "status", "persist-me"))

    path = tmp_path / "bulletin.jsonl"
    board1.flush_to_disk(path)
    assert path.exists()

    board2 = BulletinBoard()
    board2.load_from_disk(path)
    assert board2.count == 1
    assert board2.read_since(0)[0].content == "persist-me"


def test_message_board_delegation_lifecycle() -> None:
    """Test the full lifecycle of a delegation on the MessageBoard."""
    mb = MessageBoard()

    # 1. Post
    d = mb.post_delegation(
        origin_agent="agent-origin",
        target_role="reviewer",
        description="Please review my code"
    )
    assert d.status == DelegationStatus.PENDING
    assert mb.count == 1

    # 2. Query
    pending = mb.query_by_role("reviewer")
    assert len(pending) == 1
    assert pending[0].id == d.id

    # 3. Claim
    claimed = mb.claim(d.id, "agent-reviewer")
    assert claimed is not None
    assert claimed.status == DelegationStatus.CLAIMED
    assert claimed.claimed_by == "agent-reviewer"

    # Already claimed should return None
    assert mb.claim(d.id, "other-agent") is None

    # 4. Post result
    completed = mb.post_result(d.id, "agent-reviewer", "Looks good!")
    assert completed is not None
    assert completed.status == DelegationStatus.COMPLETED
    assert completed.result == "Looks good!"


def test_message_board_cleanup_expired() -> None:
    """Test that expired delegations are automatically cleaned up."""
    mb = MessageBoard()
    now = time.time()

    # One expired
    mb.post_delegation("a1", "r1", "expired-task", deadline=now - 10)
    # One active
    mb.post_delegation("a1", "r1", "active-task", deadline=now + 60)

    # Query triggers cleanup
    results = mb.query_by_role("r1")
    assert len(results) == 1
    assert results[0].description == "active-task"

    # Check the expired one's status
    all_d = mb.get_by_origin("a1")
    expired = next(d for d in all_d if d.description == "expired-task")
    assert expired.status == DelegationStatus.EXPIRED
