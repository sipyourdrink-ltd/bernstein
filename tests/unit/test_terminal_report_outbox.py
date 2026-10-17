"""Tests for the durable terminal-report outbox (worker reporting, #2808).

The outbox is the reason a terminal outcome survives a failed post, a wedged
lock, a truncated file, and a process restart. Each test below asserts on what
is actually on disk, because that file is the only thing standing between a
reaped agent and a task stuck in CLAIMED forever.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.orchestration import terminal_report_outbox as outbox_mod
from bernstein.core.orchestration.terminal_report_outbox import (
    DEFAULT_MAX_DELAY_S,
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    SETTLE_ACKED,
    TerminalReportOutbox,
    retry_delay_s,
)
from bernstein.core.persistence import file_locks
from bernstein.core.server import http_retry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _outbox(tmp_path: Path, *, node: str = "node-a", **kwargs: object) -> TerminalReportOutbox:
    return TerminalReportOutbox(tmp_path, node=node, **kwargs)  # type: ignore[arg-type]


def _lines(tmp_path: Path) -> list[dict]:
    path = _outbox(tmp_path).path
    if not path.is_file():
        return []
    body = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


class TestAppendIsDurable:
    def test_append_reaches_disk_before_delivery_is_allowed(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)

        report = outbox.append("t-1", OUTCOME_DONE, "done", delivery=("agent/s1", "sha1"))

        (raw,) = _lines(tmp_path)
        assert raw["task_id"] == "t-1"
        assert raw["outcome"] == OUTCOME_DONE
        assert raw["report_id"] == report.report_id
        # The (branch, sha) pair is frozen at queue time, not recomputed later.
        assert (raw["agent_branch"], raw["agent_sha"]) == ("agent/s1", "sha1")
        assert raw["node"] == "node-a"

    def test_append_survives_a_process_restart(self, tmp_path: Path) -> None:
        first = _outbox(tmp_path)
        first.append("t-1", OUTCOME_FAILED, "boom")

        reopened = _outbox(tmp_path)
        (report,) = reopened.pending()
        assert (report.task_id, report.outcome, report.detail) == ("t-1", OUTCOME_FAILED, "boom")

    def test_detail_is_truncated_rather_than_dropped(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)

        outbox.append("t-1", OUTCOME_FAILED, "x" * 5000)

        (report,) = _outbox(tmp_path).pending()
        assert len(report.detail) == outbox_mod._DETAIL_LIMIT

    def test_undurable_append_degrades_to_retryable_not_lost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged lock must not silently discard the outcome (#3.22.0 defect)."""

        class _Busy:
            def __enter__(self) -> None:
                raise file_locks.LockTimeout("peer holds the lock")

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(outbox_mod, "cross_process_lock", lambda *a, **k: _Busy())
        outbox = _outbox(tmp_path)

        outbox.append("t-stuck", OUTCOME_DONE, "done")

        assert _lines(tmp_path) == []
        # Still owed: retried on the next cycle, then durable once the lock frees.
        monkeypatch.setattr(outbox_mod, "cross_process_lock", file_locks.cross_process_lock)
        assert [r.task_id for r in outbox.pending()] == ["t-stuck"]
        assert [raw["task_id"] for raw in _lines(tmp_path)] == ["t-stuck"]


class TestRetrySchedule:
    def test_delays_grow_then_cap_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deterministic upper bound: full jitter is sampled at its maximum."""
        monkeypatch.setattr(http_retry.random, "uniform", lambda _a, b: b)

        delays = [retry_delay_s(attempt, 1.0, 300.0) for attempt in range(1, 12)]

        assert delays[0] == pytest.approx(1.0)
        assert delays[1] == pytest.approx(2.0)
        assert delays == sorted(delays)
        assert delays[-1] == pytest.approx(300.0)

    def test_delay_carries_jitter(self) -> None:
        samples = {retry_delay_s(4, 1.0, 300.0) for _ in range(40)}

        assert len(samples) > 1
        assert all(0.0 <= sample <= 8.0 for sample in samples)

    def test_retry_after_wins_and_is_capped(self) -> None:
        assert retry_delay_s(1, 1.0, 300.0, retry_after_s=45.0) == pytest.approx(45.0)
        assert retry_delay_s(1, 1.0, 300.0, retry_after_s=9999.0) == pytest.approx(DEFAULT_MAX_DELAY_S)
        assert retry_delay_s(1, 1.0, 300.0, retry_after_s=-5.0) == 0.0

    def test_attempt_is_stamped_and_scheduled_durable(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path, base_delay_s=5.0, max_delay_s=60.0)
        report = outbox.append("t-1", OUTCOME_DONE, "done")
        before = time.time()

        outbox.note_attempt(report.report_id, "http-503")

        (reloaded,) = _outbox(tmp_path).pending()
        assert reloaded.attempts == 1
        assert reloaded.last_status == "http-503"
        assert before <= reloaded.next_attempt_at <= before + 5.0

    def test_retries_are_bounded_in_delay_not_in_count(self, tmp_path: Path) -> None:
        """Capping attempts would reintroduce the silent discard."""
        outbox = _outbox(tmp_path, base_delay_s=0.0, max_delay_s=1.0)
        report = outbox.append("t-1", OUTCOME_DONE, "done")

        for attempt in range(1, 60):
            outbox.note_attempt(report.report_id, f"http-500-{attempt}")

        (reloaded,) = _outbox(tmp_path).pending()
        assert reloaded.attempts == 59
        assert reloaded.last_status == "http-500-59"

    def test_status_text_is_capped(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        report = outbox.append("t-1", OUTCOME_DONE, "done")

        outbox.note_attempt(report.report_id, "e" * 5000)

        (reloaded,) = _outbox(tmp_path).pending()
        assert len(reloaded.last_status) == outbox_mod._STATUS_LIMIT


class TestDueGating:
    def test_not_due_reports_are_simply_absent(self, tmp_path: Path) -> None:
        """The loop must never sleep: a deferred report is just not returned."""
        outbox = _outbox(tmp_path, base_delay_s=600.0, max_delay_s=600.0)
        report = outbox.append("t-1", OUTCOME_DONE, "done")
        outbox.note_attempt(report.report_id, "http-500", now=1_000.0)

        assert outbox.due(now=1_010.0) == []
        assert [r.task_id for r in outbox.due(now=1_700.0)] == ["t-1"]

    def test_due_orders_oldest_first(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        outbox.append("t-old", OUTCOME_DONE, "first")
        outbox.append("t-new", OUTCOME_DONE, "second")

        assert [r.task_id for r in outbox.due(now=time.time() + 1)] == ["t-old", "t-new"]

    def test_only_the_owning_node_drains_a_report(self, tmp_path: Path) -> None:
        """Two workers sharing one .sdd/ must not double-post one outcome."""
        owner = _outbox(tmp_path, node="node-a")
        owner.append("t-1", OUTCOME_DONE, "done")
        peer = _outbox(tmp_path, node="node-b")

        assert [r.task_id for r in peer.due()] == []
        assert peer.pending()  # visible, but not theirs to send
        assert [r.task_id for r in owner.due()] == ["t-1"]

    def test_backlog_survives_a_restart_without_a_retry_storm(self, tmp_path: Path) -> None:
        first = _outbox(tmp_path, base_delay_s=600.0, max_delay_s=600.0)
        report = first.append("t-1", OUTCOME_DONE, "done")
        first.note_attempt(report.report_id, "http-500", retry_after_s=600.0)

        reopened = _outbox(tmp_path)
        now = time.time()
        assert reopened.due(now=now) == []
        assert [r.task_id for r in reopened.due(now=now + 601)] == ["t-1"]


class TestSettling:
    def test_settle_compacts_the_file_so_it_cannot_replay(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        owed = outbox.append("t-1", OUTCOME_DONE, "done")
        outbox.append("t-2", OUTCOME_FAILED, "boom")

        outbox.settle(owed.report_id, SETTLE_ACKED)

        assert [raw["task_id"] for raw in _lines(tmp_path)] == ["t-2"]
        assert [r.task_id for r in _outbox(tmp_path).pending()] == ["t-2"]

    def test_settling_an_unknown_id_is_harmless(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        outbox.append("t-1", OUTCOME_DONE, "done")

        outbox.settle("not-a-report", SETTLE_ACKED)

        assert [r.task_id for r in _outbox(tmp_path).pending()] == ["t-1"]

    def test_failed_compaction_keeps_the_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Assuming the removal succeeded is exactly how outcomes get lost."""
        outbox = _outbox(tmp_path)
        report = outbox.append("t-1", OUTCOME_DONE, "done")
        monkeypatch.setattr(
            outbox_mod, "write_atomic_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )

        outbox.settle(report.report_id, SETTLE_ACKED)

        assert [r.task_id for r in _outbox(tmp_path).pending()] == ["t-1"]

    def test_busy_peer_lock_defers_instead_of_raising(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Skipping a cycle is never a lost outcome; raising into the loop is."""

        class _Busy:
            def __enter__(self) -> None:
                raise file_locks.LockTimeout("peer holds the lock")

            def __exit__(self, *exc: object) -> None:
                return None

        outbox = _outbox(tmp_path)
        outbox.append("t-1", OUTCOME_DONE, "done")
        monkeypatch.setattr(outbox_mod, "cross_process_lock", lambda *a, **k: _Busy())

        assert outbox.pending() == []
        assert outbox.due() == []


class TestUndurableReportsStayDeliverable:
    """F7: a full disk must not silently stop all reporting.

    A record whose durable write failed went into ``_volatile`` and was then
    invisible to ``due()``, so while the disk stayed full the worker posted
    nothing at all -- even with a healthy central. Before the outbox existed the
    report was delivered from memory, so this was a regression in the exact
    mechanism meant to stop outcomes being lost.

    Making those records deliverable also forces the second half: ``note_attempt``
    and ``settle`` both began by popping the volatile copy and never put it back,
    so the first retry of a not-yet-durable report would have deleted the outcome
    it was supposed to keep retrying.
    """

    @pytest.fixture
    def disk_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            outbox_mod,
            "write_atomic_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
        )

    def test_a_report_that_cannot_be_written_is_still_owed(self, tmp_path: Path, disk_full: None) -> None:
        outbox = _outbox(tmp_path)

        outbox.append("t-full", OUTCOME_DONE, "agent exited with code 0")

        assert [r.task_id for r in outbox.due()] == ["t-full"], "a healthy central must still be told"
        assert outbox.path.exists() is False

    def test_a_retry_of_an_undurable_report_does_not_delete_it(
        self, tmp_path: Path, disk_full: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stamping the backoff must not cost the only copy we hold."""
        monkeypatch.setattr(http_retry.random, "uniform", lambda _a, b: b)
        outbox = _outbox(tmp_path, base_delay_s=60.0, max_delay_s=60.0)
        report = outbox.append("t-full", OUTCOME_DONE, "done")

        outbox.note_attempt(report.report_id, "http-500", now=1_000.0)

        (held,) = [r for r in outbox._volatile.values()]
        assert held.attempts == 1
        assert held.last_status == "http-500"
        assert outbox.due(now=1_010.0) == []  # deferred by its schedule, not deleted
        assert [r.task_id for r in outbox.due(now=1_070.0)] == ["t-full"]

    def test_an_acknowledged_undurable_report_stops_being_owed(self, tmp_path: Path, disk_full: None) -> None:
        """An ack retires it even though it was never on disk."""
        outbox = _outbox(tmp_path)
        report = outbox.append("t-full", OUTCOME_DONE, "done")

        outbox.settle(report.report_id, SETTLE_ACKED)

        assert outbox.due() == []
        assert outbox._volatile == {}

    def test_a_full_disk_does_not_stop_delivery_while_the_central_is_healthy(
        self, tmp_path: Path, disk_full: None
    ) -> None:
        """The reviewer's sequence end to end: disk full, central fine, report posted."""
        from bernstein.cli.commands.worker_cmd import WorkerLoop

        loop = WorkerLoop(
            server_url="http://central:8052", name="node-a", auth_token="t", adapter="claude", workdir=tmp_path
        )
        loop._outbox._base_delay_s = 0.0
        loop._outbox._max_delay_s = 0.0
        loop._outbox.append("t-full", OUTCOME_DONE, "agent exited with code 0")

        posted: list[tuple[str, dict]] = []

        class _Healthy:
            def post(self, url: str, *, json: dict | None = None, headers: dict | None = None, timeout: float = 0.0):
                posted.append((url, dict(json or {})))
                import httpx

                return httpx.Response(200, json={"id": "t-full", "status": "done"})

        loop._report_finished(_Healthy())  # type: ignore[arg-type]

        assert [url for url, _ in posted] == ["http://central:8052/tasks/t-full/complete"]
        assert loop._outbox.due() == []


class TestOneWritePath:
    """F3/F11: every mutation must travel through the same atomic write.

    ``append`` used a different primitive from every other mutation, and the
    divergence was not cosmetic. A text append has no atomicity guarantee, so a
    crash mid-write left a fragment with no trailing newline -- and the *next*
    append concatenated straight onto it, destroying a record that was still
    owed. The two paths also disagreed about everything else: ``open(..., "a")``
    inherits the process umask where the rewrite forces 0o600, so the file was
    world-readable right after an append and owner-only after a compaction; and
    an append follows a symlink planted at the runtime path into whatever file it
    points at, where a rename replaces the link.
    """

    @pytest.fixture
    def permissive_umask(self) -> Iterator[None]:
        """Pin the umask the append path used to inherit, so the mode is not shell-dependent."""
        previous = os.umask(0o022)
        try:
            yield
        finally:
            os.umask(previous)

    def test_a_torn_tail_does_not_swallow_the_next_report(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        outbox.append("t-good", OUTCOME_DONE, "done")
        # What a SIGKILL mid-write leaves behind: bytes, no closing brace, and
        # critically no newline for the next writer to land after.
        with outbox.path.open("a", encoding="utf-8") as handle:
            handle.write('{"task_id": "t-half", "out')

        outbox.append("t-after", OUTCOME_DONE, "done")

        # The unreadable fragment is a loss; the report behind it is not.
        assert [raw["task_id"] for raw in _lines(tmp_path)] == ["t-good", "t-after"]
        assert [r.task_id for r in _outbox(tmp_path).pending()] == ["t-good", "t-after"]

    def test_the_file_is_owner_only_from_the_first_record(self, tmp_path: Path, permissive_umask: None) -> None:
        """A record carries task metadata and a claim token: 0o644 is a leak."""
        outbox = _outbox(tmp_path)

        outbox.append("t-1", OUTCOME_DONE, "done", claim_token="a" * 32)

        assert outbox.path.stat().st_mode & 0o077 == 0
        # And it must not drift when a later mutation rewrites the same file.
        owed = outbox.pending()[0]
        outbox.note_attempt(owed.report_id, "http-503")
        assert outbox.path.stat().st_mode & 0o077 == 0

    def test_a_symlink_at_the_outbox_path_is_not_written_through(self, tmp_path: Path) -> None:
        """A link planted in a shared .sdd/ must not capture the worker's writes."""
        victim = tmp_path / "elsewhere.jsonl"
        victim.write_text("not the outbox\n", encoding="utf-8")
        outbox = _outbox(tmp_path)
        outbox.path.parent.mkdir(parents=True, exist_ok=True)
        outbox.path.symlink_to(victim)

        outbox.append("t-1", OUTCOME_DONE, "done")

        assert victim.read_text(encoding="utf-8") == "not the outbox\n"
        assert not outbox.path.is_symlink()
        assert [r.task_id for r in _outbox(tmp_path).pending()] == ["t-1"]


class TestLoadsTolerantGarbage:
    def test_truncated_last_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """A crash mid-write must not blind the worker to the reports behind it."""
        outbox = _outbox(tmp_path)
        outbox.append("t-good", OUTCOME_DONE, "done")
        path = outbox.path
        path.write_text(path.read_text(encoding="utf-8") + '{"task_id": "t-half", "out', encoding="utf-8")

        (report,) = _outbox(tmp_path).pending()
        assert report.task_id == "t-good"

    def test_record_without_a_task_id_is_skipped(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        outbox.append("t-good", OUTCOME_DONE, "done")
        path = outbox.path
        path.write_text(path.read_text(encoding="utf-8") + '{"outcome": "done"}\n', encoding="utf-8")

        assert [r.task_id for r in _outbox(tmp_path).pending()] == ["t-good"]

    def test_unknown_fields_from_a_newer_worker_are_ignored(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        outbox.append("t-1", OUTCOME_DONE, "done")
        path = outbox.path
        future = {**_lines(tmp_path)[0], "from_the_future": True}
        path.write_text(json.dumps(future) + "\n", encoding="utf-8")

        (report,) = _outbox(tmp_path).pending()
        assert (report.task_id, report.outcome) == ("t-1", OUTCOME_DONE)

    def test_missing_file_is_an_empty_queue(self, tmp_path: Path) -> None:
        assert _outbox(tmp_path).pending() == []

    def test_unreadable_file_is_an_empty_queue_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outbox = _outbox(tmp_path)
        outbox.append("t-1", OUTCOME_DONE, "done")
        monkeypatch.setattr(type(outbox.path), "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("I/O error")))

        assert outbox.pending() == []

    def test_a_line_written_by_a_peer_is_visible_after_the_lock_frees(self, tmp_path: Path) -> None:
        """Every read reloads from disk, so a peer's append is never missed."""
        reader = _outbox(tmp_path, node="node-b")
        writer = _outbox(tmp_path, node="node-a")
        writer.append("t-from-a", OUTCOME_DONE, "done")

        assert [r.task_id for r in reader.pending()] == ["t-from-a"]


class TestRecordShape:
    def test_outcome_constants_are_the_wire_values(self) -> None:
        assert (OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_UNKNOWN) == ("done", "failed", "unknown")

    def test_report_id_is_stable_across_a_reload(self, tmp_path: Path) -> None:
        outbox = _outbox(tmp_path)
        report = outbox.append("t-1", OUTCOME_DONE, "done")

        (reloaded,) = _outbox(tmp_path).pending()
        assert reloaded.report_id == report.report_id

    def test_defaults_cover_a_minimal_record(self) -> None:
        report = outbox_mod.TerminalReport.from_dict({"task_id": "t-1", "outcome": OUTCOME_DONE})

        assert report.attempts == 0
        assert report.node == ""
        assert report.agent_branch is None and report.agent_sha is None
        assert report.next_attempt_at == pytest.approx(report.first_seen_at, abs=1.0)
