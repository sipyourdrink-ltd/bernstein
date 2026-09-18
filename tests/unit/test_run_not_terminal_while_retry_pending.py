"""A run is not over while a retry is still to be queued.

Measured 2026-09-03: the stale-claim releaser failed a task at 01:30:04 and
the tick loop's retry sweep created its retry at 01:30:28. For those 24 s the
board held nothing open, nothing claimed and no live agent - indistinguishable
from a finished run. The CLI polled 2 s in, printed its summary, signalled
shutdown and exited; the run went on retrying until 01:57:15.

The gap is structural, not incidental: the retry sweep (tick step 4b) iterates
the snapshot fetched at step 1, so a task the reap fails later in the same tick
is first offered to ``maybe_retry_task`` on the NEXT tick.

Two harness rules, inherited from ``test_orchestrator_liveness_false_positives``
because breaking either hides the defect:

* **The clock fakes monotonic and wall together.** A real ``time.sleep(s)``
  advances both, and the confirmation window is measured on the monotonic one.
* **``/health`` fixtures carry the ``components`` block.** A real server always
  emits it, and ``_orchestrator_liveness`` reads it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import bernstein.cli.run_bootstrap as rb
from bernstein.cli.run_bootstrap import (
    _QUIESCENCE_RETRY_CONFIRM_WINDOW_S,
    _is_quiescent,
    _retry_may_still_be_queued,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _Clock:
    """A fake clock whose monotonic and wall readings advance together."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self._base_wall = time.time()
        self._base_mono = time.monotonic()

    def time(self) -> float:
        return self._base_wall + self.elapsed

    def monotonic(self) -> float:
        return self._base_mono + self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def _health() -> dict[str, Any]:
    """A ``/health`` payload shaped like the real one, reporting a live run."""
    return {
        "status": "ok",
        "agent_count": 0,
        "components": {
            "server": {"status": "ok"},
            "spawner": {"status": "ok", "pid": 1, "detail": ""},
            "database": {"status": "ok", "type": "TaskStore", "detail": ""},
            "agents": {"status": "ok", "active": 0, "detail": "no active agents"},
        },
    }


#: One poll's worth of server state: the ``/status`` payload and the
#: ``/tasks/counts`` histogram, or ``None`` for a server that does not serve
#: the histogram route.
Poll = tuple[dict[str, Any], "dict[str, Any] | None"]


def _drive(
    clock: _Clock,
    polls: list[Poll],
    *,
    timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
) -> tuple[dict[str, Any] | None, int]:
    """Drive the real wait loop over a sequence of polls. The last one repeats.

    Returns ``(verdict, polls_served)``.
    """
    served = {"n": 0}

    def fake_get(path: str) -> Any:
        index = min(max(served["n"] - 1, 0), len(polls) - 1)
        if path == "/status":
            served["n"] += 1
            return dict(polls[min(served["n"] - 1, len(polls) - 1)][0])
        if path == "/health":
            return _health()
        if path == "/tasks/counts":
            counts = polls[index][1]
            # A server without the route answers with an error body, which
            # `_looks_like_status_histogram` must reject rather than read as
            # an all-zero histogram.
            return dict(counts) if counts is not None else {"detail": "Not Found"}
        return None

    with (
        patch.object(rb, "server_get", side_effect=fake_get),
        patch.object(rb, "time", clock),
        patch.object(rb, "_signal_orchestrator_shutdown"),
    ):
        verdict = rb._wait_for_run_completion(poll_interval_s=poll_interval_s, timeout_s=timeout_s)
    return verdict, served["n"]


def _full(**counts: int) -> dict[str, Any]:
    """A complete per-status histogram with every bucket the CLI reads."""
    base = {"open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
    base.update(counts)
    return {"total": sum(base.values()), **base}


#: Quiescent, but one task failed: its retry is queued on a later tick.
QUIESCENT_WITH_FAILED: Poll = ({"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 1}, _full(failed=1))
#: The retry has landed. The board is no longer quiescent.
RETRY_LANDED: Poll = ({"total": 2, "open": 1, "claimed": 0, "done": 0, "failed": 1}, _full(open=1, failed=1))
#: Every task succeeded. Nothing can retry.
ALL_SUCCEEDED: Poll = ({"total": 2, "open": 0, "claimed": 0, "done": 2, "failed": 0}, _full(done=2))
#: A cancelled task ended without delivering, but is never retried.
QUIESCENT_WITH_CANCELLED: Poll = (
    {"total": 2, "open": 0, "claimed": 0, "done": 1, "failed": 0},
    {**_full(done=1), "cancelled": 1, "total": 2},
)


class TestTheWaitLoopWaitsOutThePendingRetry:
    def test_a_quiescent_board_holding_a_failed_task_is_re_observed_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measured defect: one quiescent poll ended the run 22 s early."""
        monkeypatch.chdir(tmp_path)
        clock = _Clock()
        verdict, _ = _drive(clock, [QUIESCENT_WITH_FAILED])

        assert verdict is not None, "the run does end once the window passes with no retry in sight"
        assert clock.elapsed >= _QUIESCENCE_RETRY_CONFIRM_WINDOW_S, (
            f"the verdict came after {clock.elapsed}s; a quiescent board holding a failed task "
            f"must be re-observed across {_QUIESCENCE_RETRY_CONFIRM_WINDOW_S}s first, because the "
            "retry is created on a later orchestrator tick"
        )

    def test_the_retry_that_arrives_inside_the_window_keeps_the_run_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the window is for: the run continues, and the CLI stays attached."""
        monkeypatch.chdir(tmp_path)
        clock = _Clock()
        verdict, served = _drive(
            clock,
            [QUIESCENT_WITH_FAILED, QUIESCENT_WITH_FAILED, RETRY_LANDED],
            timeout_s=120.0,
        )

        assert verdict is None, "a run whose retry landed is still in flight and must get no verdict"
        assert served > 3, "the loop must keep polling after the retry appears"

    def test_an_all_successful_run_still_exits_on_the_first_observation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window must not tax the ordinary case: nothing failed, nothing can retry."""
        monkeypatch.chdir(tmp_path)
        clock = _Clock()
        verdict, served = _drive(clock, [ALL_SUCCEEDED])

        assert verdict is not None
        assert served == 1, "an all-succeeded run is terminal on the first poll"
        assert clock.elapsed < _QUIESCENCE_RETRY_CONFIRM_WINDOW_S

    def test_the_check_still_holds_when_the_full_histogram_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`/status` carries a real `failed` bucket, so the fallback is a count, not a guess.

        This is the poll the original defect fired on. Answering from the full
        histogram alone would go silent here and exit immediately.
        """
        monkeypatch.chdir(tmp_path)
        clock = _Clock()
        verdict, _ = _drive(clock, [(QUIESCENT_WITH_FAILED[0], None)])

        assert verdict is not None
        assert clock.elapsed >= _QUIESCENCE_RETRY_CONFIRM_WINDOW_S, (
            "a missing /tasks/counts must not disable the confirmation window: unlike "
            "in_progress and orphaned, `failed` has a real bucket in /status"
        )

    def test_a_cancelled_task_does_not_hold_a_finished_run_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only `failed` can grow new work. Cancelled, refused and abandoned cannot.

        The tick loop's retry sweep reads `tasks_by_status["failed"]` and nothing
        else, so waiting on any other unsuccessful terminal status delays a run
        that is genuinely over.
        """
        monkeypatch.chdir(tmp_path)
        clock = _Clock()
        verdict, served = _drive(clock, [QUIESCENT_WITH_CANCELLED])

        assert verdict is not None
        assert served == 1, "a cancelled task is never retried; the run is over on the first poll"
        assert clock.elapsed < _QUIESCENCE_RETRY_CONFIRM_WINDOW_S


class TestThePendingRetryPredicate:
    def test_a_failed_task_means_a_retry_may_be_pending(self) -> None:
        assert _retry_may_still_be_queued({}, _full(failed=1)) is True

    def test_an_all_successful_run_has_nothing_to_wait_for(self) -> None:
        assert _retry_may_still_be_queued({}, _full(done=2)) is False

    def test_a_status_only_payload_still_answers(self) -> None:
        """`/status` reports failed, so the fallback is a real count."""
        assert _retry_may_still_be_queued({"total": 1, "failed": 1}, None) is True
        assert _retry_may_still_be_queued({"total": 1, "failed": 0}, None) is False

    def test_a_non_histogram_error_body_falls_back_to_status(self) -> None:
        assert _retry_may_still_be_queued({"total": 1, "failed": 1}, None) is True

    def test_statuses_that_never_retry_do_not_hold_the_run(self) -> None:
        """Only the retry sweep's own input counts, and it reads `failed` alone."""
        for status in ("cancelled", "refused", "abandoned", "blocked_by_abandon", "blocked_by_failed_dep"):
            assert _retry_may_still_be_queued({}, {**_full(done=1), status: 1}) is False, status


class TestQuiescencePredicateUnchanged:
    """`_is_quiescent` still answers only 'right now', as documented."""

    def test_board_with_nothing_outstanding_is_quiescent(self) -> None:
        assert _is_quiescent(
            total=4, open_count=0, claimed_count=0, agent_count=0, n_incomplete=0, counts_are_complete=True
        )

    def test_an_in_progress_task_vetoes_it(self) -> None:
        assert not _is_quiescent(
            total=4, open_count=0, claimed_count=0, agent_count=0, n_incomplete=1, counts_are_complete=True
        )
