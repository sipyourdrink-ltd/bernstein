"""Tests for bernstein.core.bulletin — BulletinBoard."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from bernstein.core.bulletin import BulletinBoard, BulletinMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    agent_id: str = "agent-1",
    msg_type: str = "status",
    content: str = "hello",
    timestamp: float = 0.0,
    cell_id: str | None = None,
) -> BulletinMessage:
    return BulletinMessage(
        agent_id=agent_id,
        type=msg_type,  # type: ignore[arg-type]
        content=content,
        timestamp=timestamp,
        cell_id=cell_id,
    )


# ---------------------------------------------------------------------------
# post / append
# ---------------------------------------------------------------------------


class TestPost:
    def test_auto_timestamp_filled_in(self) -> None:
        board = BulletinBoard()
        before = time.time()
        stored = board.post(_msg())
        after = time.time()
        assert before <= stored.timestamp <= after

    def test_explicit_timestamp_preserved(self) -> None:
        board = BulletinBoard()
        ts = 1_700_000_000.0
        stored = board.post(_msg(timestamp=ts))
        assert stored.timestamp == ts

    def test_count_increments(self) -> None:
        board = BulletinBoard()
        assert board.count == 0
        board.post(_msg())
        board.post(_msg())
        assert board.count == 2

    def test_post_returns_message(self) -> None:
        board = BulletinBoard()
        msg = _msg(content="ping")
        stored = board.post(msg)
        assert stored.content == "ping"


# ---------------------------------------------------------------------------
# read_since
# ---------------------------------------------------------------------------


class TestReadSince:
    def test_empty_board(self) -> None:
        board = BulletinBoard()
        assert board.read_since(0.0) == []

    def test_returns_messages_after_ts(self) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0))
        board.post(_msg(timestamp=2.0))
        board.post(_msg(timestamp=3.0))
        result = board.read_since(1.5)
        assert len(result) == 2
        assert all(m.timestamp > 1.5 for m in result)

    def test_boundary_exclusive(self) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=5.0))
        # ts=5.0 is NOT > 5.0, so it's excluded
        assert board.read_since(5.0) == []
        # ts=5.0 IS > 4.9
        assert len(board.read_since(4.9)) == 1


# ---------------------------------------------------------------------------
# read_by_type
# ---------------------------------------------------------------------------


class TestReadByType:
    def test_filters_by_type(self) -> None:
        board = BulletinBoard()
        board.post(_msg(msg_type="alert", timestamp=1.0))
        board.post(_msg(msg_type="blocker", timestamp=2.0))
        board.post(_msg(msg_type="alert", timestamp=3.0))

        alerts = board.read_by_type("alert")
        assert len(alerts) == 2
        assert all(m.type == "alert" for m in alerts)

    def test_no_match_returns_empty(self) -> None:
        board = BulletinBoard()
        board.post(_msg(msg_type="status", timestamp=1.0))
        assert board.read_by_type("finding") == []

    def test_all_types(self) -> None:
        board = BulletinBoard()
        for t in ("alert", "blocker", "finding", "status", "dependency"):
            board.post(_msg(msg_type=t, timestamp=1.0))  # type: ignore[arg-type]
        for t in ("alert", "blocker", "finding", "status", "dependency"):
            assert len(board.read_by_type(t)) == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# read_by_cell
# ---------------------------------------------------------------------------


class TestReadByCell:
    def test_filters_by_cell_id(self) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0, cell_id="cell-A"))
        board.post(_msg(timestamp=2.0, cell_id="cell-B"))
        board.post(_msg(timestamp=3.0, cell_id="cell-A"))

        cell_a = board.read_by_cell("cell-A")
        assert len(cell_a) == 2
        assert all(m.cell_id == "cell-A" for m in cell_a)

    def test_excludes_no_cell_id(self) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0, cell_id=None))
        board.post(_msg(timestamp=2.0, cell_id="cell-X"))
        assert len(board.read_by_cell("cell-X")) == 1

    def test_unknown_cell_empty(self) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0, cell_id="cell-A"))
        assert board.read_by_cell("does-not-exist") == []


# ---------------------------------------------------------------------------
# flush_to_disk / load_from_disk
# ---------------------------------------------------------------------------


class TestDiskPersistence:
    def test_flush_writes_jsonl(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0, content="first"))
        board.post(_msg(timestamp=2.0, content="second"))
        path = tmp_path / "board.jsonl"
        n = board.flush_to_disk(path)
        assert n == 2
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        data = json.loads(lines[0])
        assert data["content"] == "first"

    def test_flush_empty_board_returns_zero(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        n = board.flush_to_disk(tmp_path / "empty.jsonl")
        assert n == 0

    def test_flush_appends_not_overwrites(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0, content="first"))
        path = tmp_path / "board.jsonl"
        board.flush_to_disk(path)

        board2 = BulletinBoard()
        board2.post(_msg(timestamp=2.0, content="second"))
        board2.flush_to_disk(path)

        lines = path.read_text().splitlines()
        assert len(lines) == 2

    def test_load_from_disk_populates_board(self, tmp_path: Path) -> None:
        # Write a JSONL file manually
        path = tmp_path / "board.jsonl"
        path.write_text(
            '{"agent_id": "a1", "type": "alert", "content": "hi", "timestamp": 1.0, "cell_id": null}\n'
            '{"agent_id": "a2", "type": "status", "content": "ok", "timestamp": 2.0, "cell_id": "c1"}\n'
        )
        board = BulletinBoard()
        n = board.load_from_disk(path)
        assert n == 2
        assert board.count == 2
        assert board.read_by_type("alert")[0].agent_id == "a1"

    def test_load_skips_duplicates(self, tmp_path: Path) -> None:
        path = tmp_path / "board.jsonl"
        path.write_text('{"agent_id": "a1", "type": "status", "content": "x", "timestamp": 1.0, "cell_id": null}\n')
        board = BulletinBoard()
        board.post(_msg(timestamp=1.0))  # same timestamp
        n = board.load_from_disk(path)
        assert n == 0
        assert board.count == 1  # no duplicate

    def test_load_missing_file_returns_zero(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        n = board.load_from_disk(tmp_path / "nonexistent.jsonl")
        assert n == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_posts_no_corruption(self) -> None:
        board = BulletinBoard()
        n_threads = 20
        n_per_thread = 50

        def post_many(thread_id: int) -> None:
            for i in range(n_per_thread):
                board.post(
                    _msg(
                        agent_id=f"agent-{thread_id}",
                        content=f"msg-{i}",
                        timestamp=float(thread_id * 1000 + i) + 0.001,
                    )
                )

        threads = [threading.Thread(target=post_many, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert board.count == n_threads * n_per_thread
