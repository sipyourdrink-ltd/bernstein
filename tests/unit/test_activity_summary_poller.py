"""Tests for ActivitySummaryPoller — periodic bulletin-board broadcasts."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bernstein.core.activity_summary_poller import ActivitySummaryPoller
from bernstein.core.bulletin import BulletinBoard

from bernstein.tui.activity_tracker import ActivitySession


@pytest.fixture()
def activity_dir(tmp_path: Path) -> Path:
    return tmp_path / "metrics"


@pytest.fixture()
def session(activity_dir: Path) -> ActivitySession:
    return ActivitySession(activity_dir)


@pytest.fixture()
def board() -> BulletinBoard:
    return BulletinBoard()


class TestPollOnce:
    def test_posts_idle_summary_when_no_activity(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="agent-1", session=session, board=board)
        poller.poll_once()
        result = board.get_latest_activity_summary("agent-1")
        assert result is not None
        assert result.summary == "idle no recent activity"

    def test_posts_in_progress_when_active(self, session: ActivitySession, board: BulletinBoard) -> None:
        session.start_activity("coding", "Implement feature")
        poller = ActivitySummaryPoller(agent_id="agent-2", session=session, board=board)
        poller.poll_once()
        result = board.get_latest_activity_summary("agent-2")
        assert result is not None
        assert result.summary == "coding in progress"

    def test_posts_completed_after_stop(self, session: ActivitySession, board: BulletinBoard) -> None:
        session.start_activity("testing", "Run tests")
        time.sleep(0.02)
        session.stop_activity()
        poller = ActivitySummaryPoller(agent_id="agent-3", session=session, board=board)
        poller.poll_once()
        result = board.get_latest_activity_summary("agent-3")
        assert result is not None
        assert result.summary == "testing task completed"

    def test_latest_summary_overwrites_previous(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="agent-4", session=session, board=board)
        poller.poll_once()  # idle
        session.start_activity("coding", "Write code")
        poller.poll_once()  # coding in progress
        result = board.get_latest_activity_summary("agent-4")
        assert result is not None
        assert result.summary == "coding in progress"

    def test_returns_posted_summary(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="agent-5", session=session, board=board)
        returned = poller.poll_once()
        assert returned.agent_id == "agent-5"
        assert returned.summary == "idle no recent activity"

    def test_summary_has_fresh_timestamp(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="agent-6", session=session, board=board)
        before = time.time()
        poller.poll_once()
        after = time.time()
        result = board.get_latest_activity_summary("agent-6")
        assert result is not None
        assert before <= result.timestamp <= after


class TestPollerThread:
    def test_start_spawns_running_thread(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="bg-1", session=session, board=board, interval=60.0)
        assert not poller.is_running
        poller.start()
        try:
            assert poller.is_running
        finally:
            poller.stop()

    def test_stop_terminates_thread(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="bg-2", session=session, board=board, interval=60.0)
        poller.start()
        poller.stop(timeout=2.0)
        assert not poller.is_running

    def test_start_is_idempotent(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="bg-3", session=session, board=board, interval=60.0)
        poller.start()
        poller.start()  # second call should not raise or spawn a second thread
        try:
            assert poller.is_running
        finally:
            poller.stop()

    def test_periodic_poll_updates_board(self, activity_dir: Path, board: BulletinBoard) -> None:
        """Summary reflects activity changes across polling cycles."""

        def _wait_for_summary(expected: str, timeout: float = 2.0) -> None:
            """Poll up to ``timeout`` seconds for the board to show ``expected``.

            The old version of this test slept for 0.12s and read once,
            which was flaky on slow CI (macOS runner observed 2026-04-11)
            whenever the poll thread had not yet completed its next tick.
            Swap the fixed sleep for a bounded spin that succeeds as soon
            as the summary matches — identical semantics on a fast box,
            tolerant of a delayed tick on a slow one.
            """
            deadline = time.time() + timeout
            last_seen: str | None = None
            while time.time() < deadline:
                current = board.get_latest_activity_summary("bg-4")
                if current is not None:
                    last_seen = current.summary
                    if current.summary == expected:
                        return
                time.sleep(0.01)
            raise AssertionError(
                f"summary did not reach {expected!r} within {timeout}s "
                f"(last seen: {last_seen!r})"
            )

        session = ActivitySession(activity_dir)
        poller = ActivitySummaryPoller(agent_id="bg-4", session=session, board=board, interval=0.05)
        poller.start()
        try:
            # Wait for first poll (idle)
            _wait_for_summary("idle no recent activity")

            # Start an activity and wait for next poll cycle
            session.start_activity("coding", "Some task")
            _wait_for_summary("coding in progress")
        finally:
            poller.stop()

    def test_summary_is_3_to_5_words_from_thread(self, session: ActivitySession, board: BulletinBoard) -> None:
        poller = ActivitySummaryPoller(agent_id="bg-5", session=session, board=board, interval=0.05)
        poller.start()
        try:
            time.sleep(0.12)
            result = board.get_latest_activity_summary("bg-5")
            assert result is not None
            words = result.summary.split()
            assert 3 <= len(words) <= 5
        finally:
            poller.stop()


class TestWriteToDisk:
    def test_poll_once_writes_json_file_when_sdd_dir_set(
        self, tmp_path: Path, session: ActivitySession, board: BulletinBoard
    ) -> None:
        sdd_dir = tmp_path / ".sdd"
        poller = ActivitySummaryPoller(agent_id="disk-1", session=session, board=board, sdd_dir=sdd_dir)
        poller.poll_once()
        out = sdd_dir / "runtime" / "activity_summaries" / "disk-1.json"
        assert out.exists()
        import json

        data = json.loads(out.read_text())
        assert data["agent_id"] == "disk-1"
        assert data["summary"] == "idle no recent activity"
        assert "timestamp" in data

    def test_poll_once_no_disk_write_without_sdd_dir(
        self, tmp_path: Path, session: ActivitySession, board: BulletinBoard
    ) -> None:
        poller = ActivitySummaryPoller(agent_id="nodisk-1", session=session, board=board)
        poller.poll_once()
        # No file should exist anywhere for nodisk-1
        candidates = list(tmp_path.rglob("nodisk-1.json"))
        assert not candidates

    def test_disk_file_reflects_active_session(self, tmp_path: Path, activity_dir: Path, board: BulletinBoard) -> None:
        session = ActivitySession(activity_dir)
        session.start_activity("coding", "Write feature")
        sdd_dir = tmp_path / ".sdd"
        poller = ActivitySummaryPoller(agent_id="disk-2", session=session, board=board, sdd_dir=sdd_dir)
        poller.poll_once()
        import json

        out = sdd_dir / "runtime" / "activity_summaries" / "disk-2.json"
        data = json.loads(out.read_text())
        assert data["summary"] == "coding in progress"

    def test_disk_file_overwritten_on_next_poll(self, tmp_path: Path, activity_dir: Path, board: BulletinBoard) -> None:
        session = ActivitySession(activity_dir)
        sdd_dir = tmp_path / ".sdd"
        poller = ActivitySummaryPoller(agent_id="disk-3", session=session, board=board, sdd_dir=sdd_dir)
        poller.poll_once()  # idle
        session.start_activity("testing", "Run tests")
        poller.poll_once()  # testing in progress
        import json

        out = sdd_dir / "runtime" / "activity_summaries" / "disk-3.json"
        data = json.loads(out.read_text())
        assert data["summary"] == "testing in progress"
