"""Regression tests for worker-side terminal-state reporting (#2808).

Two defects live here. The first: ``_complete_task`` / ``_fail_task`` had zero
call sites, so a reaped agent left its task CLAIMED forever. The second, found
in 3.22.0: reporting existed but was *silent-loss* - ``_report_finished``
cleared its in-memory queue before looking at the response, so a 500, a 409, or
a dead socket discarded the outcome exactly as thoroughly as never posting it.

So every test below asserts on the durable outbox, not on a queue object: a
report is queued before delivery, and it stops being pending only when the
central acknowledges it or the task record settles it.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from bernstein.cli.commands import worker_cmd
from bernstein.cli.commands.worker_cmd import WorkerLoop
from bernstein.core.orchestration.terminal_report_outbox import (
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
)

if TYPE_CHECKING:
    from pathlib import Path

SERVER = "http://central:8052"


class FakeCentral:
    """Scripted stand-in for the worker's ``httpx.Client``.

    Responses are queued per URL and the last one repeats, so a test scripts
    "500 twice, then 200" without bookkeeping. An unscripted URL raises: a
    compensating post the code was not supposed to make fails the test loudly
    instead of silently returning a MagicMock.
    """

    def __init__(self) -> None:
        self.routes: dict[str, list[object]] = {}
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[str] = []

    def script(self, url: str, *replies: object) -> None:
        self.routes.setdefault(url, []).extend(replies)

    def _answer(self, url: str) -> object:
        queue = self.routes.get(url)
        if not queue:
            raise AssertionError(f"unexpected request for {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None, timeout: float = 0.0):
        self.post_calls.append((url, dict(json or {})))
        reply = self._answer(url)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url: str, *, headers: dict | None = None, timeout: float = 0.0):
        self.get_calls.append(url)
        reply = self._answer(url)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def posted(self, task_id: str, verb: str) -> list[dict]:
        return [body for url, body in self.post_calls if url == f"{SERVER}/tasks/{task_id}/{verb}"]

    def post_urls(self) -> list[str]:
        return [url for url, _ in self.post_calls]


def _ack(task_id: str, status: str = "done") -> httpx.Response:
    """A response that really acknowledges this task."""
    return httpx.Response(200, json={"id": task_id, "status": status})


def _http(code: int, *, body: object = None, headers: dict | None = None) -> httpx.Response:
    payload = {} if body is None else body
    if isinstance(payload, dict):
        return httpx.Response(code, json=payload, headers=headers)
    return httpx.Response(code, content=str(payload).encode(), headers=headers)


def _task(task_id: str, status: str) -> httpx.Response:
    """The authoritative task record the central answers with."""
    return httpx.Response(200, json={"id": task_id, "status": status})


def _make_loop(tmp_path: Path, *, name: str = "test-node") -> WorkerLoop:
    return WorkerLoop(
        server_url=SERVER,
        name=name,
        auth_token="secret-token",
        adapter="claude",
        workdir=tmp_path,
    )


def _queued(loop: WorkerLoop) -> list:
    return loop._outbox.pending()


def _retry_now(loop: WorkerLoop) -> None:
    """Collapse the backoff floor so a retried report is due on the next cycle.

    Production caps the *delay*, never the count; tests that exercise several
    delivery attempts in one second would otherwise sit inside the first
    backoff window and never re-post.
    """
    loop._outbox._base_delay_s = 0.0
    loop._outbox._max_delay_s = 0.0


def _reap_with_exit_code(loop: WorkerLoop, monkeypatch: pytest.MonkeyPatch, status: int, code: int) -> bool:
    monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (pid, status))
    monkeypatch.setattr(worker_cmd.os, "waitstatus_to_exitcode", lambda status: code)
    return loop._reap_finished()


class TestReapQueuesDurableOutcome:
    def test_clean_exit_queues_completion_durable_before_delivery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-1": 111}
        loop._task_sessions["task-1"] = "sess-1"
        monkeypatch.setattr(loop, "_push_agent_work", lambda tid: ("agent/sess-1", "abc123"))

        assert _reap_with_exit_code(loop, monkeypatch, 0, 0) is True
        assert loop._active_tasks == {}
        (report,) = _queued(loop)
        assert (report.task_id, report.outcome, report.detail) == ("task-1", OUTCOME_DONE, "agent exited with code 0")
        assert (report.agent_branch, report.agent_sha) == ("agent/sess-1", "abc123")
        # Durable, not merely in memory: this is what survives the crash.
        assert "task-1" in loop._outbox.path.read_text(encoding="utf-8")

    def test_clean_exit_push_failure_becomes_task_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When push fails, the task is failed, not completed (#6538)."""
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-1b": 112}
        loop._task_sessions["task-1b"] = "sess-1b"
        monkeypatch.setattr(loop, "_push_agent_work", lambda tid: None)

        assert _reap_with_exit_code(loop, monkeypatch, 0, 0) is True
        (report,) = _queued(loop)
        assert report.task_id == "task-1b"
        assert report.outcome == OUTCOME_FAILED
        assert "delivery failed" in report.detail
        assert report.agent_branch is None and report.agent_sha is None

    def test_nonzero_exit_queues_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-2": 222}

        assert _reap_with_exit_code(loop, monkeypatch, 3 << 8, 3) is True
        (report,) = _queued(loop)
        assert (report.outcome, report.detail) == (OUTCOME_FAILED, "agent exited with code 3")

    def test_signal_kill_queues_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-3": 333}

        assert _reap_with_exit_code(loop, monkeypatch, 9, -9) is True
        (report,) = _queued(loop)
        assert (report.outcome, report.detail) == (OUTCOME_FAILED, "agent exited with code -9")

    def test_still_running_agent_is_not_reaped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-4": 444}
        monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (0, 0))
        monkeypatch.setattr("bernstein.core.platform_compat.process_alive", lambda pid: True)

        assert loop._reap_finished() is False
        assert loop._active_tasks == {"task-4": 444}
        assert _queued(loop) == []

    def test_already_reaped_child_is_unknown_not_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An exit status the worker never read proves nothing (#3.22.0 defect).

        The old code reported this as a success and pushed the branch. This is
        now the *handle-less* case -- a worker that restarted, or a backend that
        gives no waitable process -- and it is queued as unknown for the server's
        task record to decide. A child the worker spawned and can still poll is
        decided by its real status instead (see TestReapReadsTheRetainedHandle).
        """
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-5": 555}
        loop._task_sessions["task-5"] = "sess-5"

        def _raise(pid: int, flags: int) -> tuple[int, int]:
            raise ChildProcessError

        monkeypatch.setattr(worker_cmd.os, "waitpid", _raise)
        pushes: list[str] = []
        monkeypatch.setattr(loop, "_push_agent_work", lambda tid: pushes.append(tid) or ("agent/sess-5", "def456"))

        assert loop._reap_finished() is True
        (report,) = _queued(loop)
        assert report.outcome == OUTCOME_UNKNOWN
        assert "exit status unavailable" in report.detail
        assert pushes == []


class TestReapReadsTheRetainedHandle:
    """F4: the exit status must be read from the handle the worker keeps.

    ``_spawn_agent`` used to build its ``AgentSpawner`` as a local, so the
    ``Popen`` it stored in ``_procs`` went out of scope with the method. A
    garbage-collected ``Popen`` is still on ``subprocess._active``, so the *next*
    ``Popen`` created anywhere in the worker reaps it through ``_cleanup()`` --
    which is exactly what ``_push_agent_work``'s git calls do for another task.
    The worker's ``os.waitpid`` then raised ``ChildProcessError``, the outcome
    became UNKNOWN, the branch was never pushed, and the task was failed: work
    that succeeded was reported as having failed.
    """

    def test_a_status_subprocess_consumed_first_is_still_reported_as_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = _make_loop(tmp_path)
        # A real child with a real exit status, already waited on by its Popen --
        # the state _cleanup() leaves the OS in. os.waitpid can no longer see it.
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        assert proc.wait() == 0

        loop._active_tasks = {"task-11": proc.pid}
        loop._task_sessions["task-11"] = "sess-11"
        loop._agent_procs["task-11"] = proc
        monkeypatch.setattr(loop, "_push_agent_work", lambda tid: ("agent/sess-11", "sha-11"))

        assert loop._reap_finished() is True
        (report,) = _queued(loop)
        assert report.outcome == OUTCOME_DONE
        assert report.detail == "agent exited with code 0"
        # The whole point: a clean exit still delivers.
        assert (report.agent_branch, report.agent_sha) == ("agent/sess-11", "sha-11")
        # And the handle leaves with the slot, so a restarted worker cannot
        # mistake a stale Popen for a live agent.
        assert loop._agent_procs == {}

    def test_a_failing_handle_is_reported_as_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
        assert proc.wait() == 3

        loop._active_tasks = {"task-12": proc.pid}
        loop._agent_procs["task-12"] = proc
        monkeypatch.setattr(loop, "_push_agent_work", lambda tid: pytest.fail("a failed agent must not push"))

        assert loop._reap_finished() is True
        (report,) = _queued(loop)
        assert report.outcome == OUTCOME_FAILED
        assert report.detail == "agent exited with code 3"

    def test_a_still_running_handle_is_not_reaped(self, tmp_path: Path) -> None:
        """Polling a live handle must not block the loop or free the slot."""
        loop = _make_loop(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            loop._active_tasks = {"task-13": proc.pid}
            loop._agent_procs["task-13"] = proc

            assert loop._reap_finished() is False
            assert loop._active_tasks == {"task-13": proc.pid}
            assert _queued(loop) == []
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class TestAcknowledgeBeforeSettling:
    def test_acknowledged_completion_settles_and_compacts(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-ok", OUTCOME_DONE, "done", delivery=("agent/s1", "sha1"))
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-ok/complete", _ack("t-ok"))

        loop._report_finished(client)

        assert client.posted("t-ok", "complete") == [
            {"result_summary": "done", "agent_branch": "agent/s1", "agent_sha": "sha1"}
        ]
        assert _queued(loop) == []
        assert loop._outbox.path.read_text(encoding="utf-8").strip() == ""

    def test_acknowledged_failure_settles(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-bad", OUTCOME_FAILED, "boom")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-bad/fail", _ack("t-bad", "failed"))

        loop._report_finished(client)

        assert client.posted("t-bad", "fail") == [{"reason": "boom"}]
        assert _queued(loop) == []

    def test_complete_without_delivery_omits_branch_fields(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-local", OUTCOME_DONE, "done locally")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-local/complete", _ack("t-local"))

        loop._report_finished(client)

        assert client.posted("t-local", "complete") == [{"result_summary": "done locally"}]

    def test_noop_when_nothing_queued(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        client = FakeCentral()
        loop._report_finished(client)
        assert client.post_calls == []

    def test_200_with_unparseable_body_is_not_an_ack(self, tmp_path: Path) -> None:
        """A proxy error page must not retire a real outcome."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-page", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-page/complete", _http(200, body="<html>cached</html>"))

        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status == "http-200-unverifiable"

    def test_200_for_a_different_task_is_not_an_ack(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-mine", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-mine/complete", _ack("t-someone-else"))

        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status == "http-200-unverifiable"


class TestTransientFailuresKeepTheReport:
    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_server_error_leaves_report_queued_for_retry(self, tmp_path: Path, code: int) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-5xx", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-5xx/complete", _http(code))

        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.attempts == 1
        assert report.last_status == f"http-{code}"
        # Still owed, and scheduled to be tried again rather than dropped.
        assert [r.task_id for r in loop._outbox.due(now=time.time() + 3600)] == ["t-5xx"]

    def test_transport_failure_leaves_report_queued(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-down", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-down/complete", httpx.ConnectError("connection refused"))

        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status == "transport-failure"

    def test_recovery_after_an_outage_delivers_once(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        _retry_now(loop)
        loop._outbox.append("t-blip", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-blip/complete", httpx.ConnectError("down"), _http(503), _ack("t-blip"))

        for _ in range(3):
            loop._report_finished(client)

        assert len(client.posted("t-blip", "complete")) == 3
        assert _queued(loop) == []

    def test_429_honours_retry_after(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-limited", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-limited/complete", _http(429, headers={"Retry-After": "7"}))

        before = time.time()
        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status == "http-429"
        assert before + 6 <= report.next_attempt_at <= before + 8
        # Not due yet inside the requested window.
        assert loop._outbox.due(now=before + 1) == []

    def test_auth_rejection_holds_the_report(self, tmp_path: Path) -> None:
        """A bad token is not a reason to lose an outcome; hold and retry."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-auth", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-auth/complete", _http(401))

        before = time.time()
        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status == "http-401"
        assert report.next_attempt_at >= before + worker_cmd._AUTH_HOLD_S - 1

    def test_not_yet_due_reports_are_not_posted(self, tmp_path: Path) -> None:
        """The backoff gate is enforced by omission - the loop never sleeps."""
        loop = _make_loop(tmp_path)
        report = loop._outbox.append("t-hold", OUTCOME_DONE, "done")
        loop._outbox.note_attempt(report.report_id, "http-500", retry_after_s=120.0)
        client = FakeCentral()

        loop._report_finished(client)

        assert client.post_calls == []


class TestReconcileRejectedReports:
    def test_409_with_matching_server_state_is_a_duplicate(self, tmp_path: Path) -> None:
        """The earlier attempt landed; retrying would only double-post."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-dupe", OUTCOME_DONE, "done", delivery=("agent/s1", "sha1"))
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-dupe/complete", _http(409, body={"error": "already terminal"}))
        client.script(f"{SERVER}/tasks/t-dupe", _task("t-dupe", "done"))

        loop._report_finished(client)

        assert len(client.posted("t-dupe", "complete")) == 1
        assert client.get_calls == [f"{SERVER}/tasks/t-dupe"]
        assert _queued(loop) == []

    def test_409_with_conflicting_state_never_overwrites_the_server(self, tmp_path: Path) -> None:
        """The janitor failed this task; our "done" must not post a /fail to match."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-conflict", OUTCOME_DONE, "done", delivery=("agent/s1", "sha1"))
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-conflict/complete", _http(409, body={"error": "already terminal"}))
        client.script(f"{SERVER}/tasks/t-conflict", _task("t-conflict", "failed"))

        loop._report_finished(client)

        assert client.posted("t-conflict", "fail") == []
        assert _queued(loop) == []

    def test_409_while_task_is_still_live_retries(self, tmp_path: Path) -> None:
        """A version-conflict 409 is not a settled outcome: keep owing it."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-race", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-race/complete", _http(409, body={"error": "version conflict"}))
        client.script(f"{SERVER}/tasks/t-race", _task("t-race", "claimed"))

        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status == "http-409:claimed"

    def test_409_that_cannot_be_read_is_retried_not_dropped(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-blind", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-blind/complete", _http(409))
        client.script(f"{SERVER}/tasks/t-blind", httpx.ConnectError("central is down"))

        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.last_status.endswith("-unreconcilable")

    def test_409_for_a_deleted_task_is_held_until_the_absence_is_repeated(self, tmp_path: Path) -> None:
        """One "not found" is not proof: a wrong tenant answers with the same body.

        ``_require_task_access`` raises 404 ``Task '<id>' not found`` for a task
        that exists in another tenant, which is byte-for-byte the shape a deleted
        task produces. Dropping the outcome on the first sighting is how a token
        rotated into the wrong tenant silently loses every queued report.
        """
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-gone", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-gone/complete", _http(409))
        client.script(f"{SERVER}/tasks/t-gone", _http(404, body={"detail": "Task 't-gone' not found"}))

        before = time.time()
        loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.absences == 1
        assert report.next_attempt_at >= before + worker_cmd._GONE_HOLD_S - 1

    def test_a_repeatedly_absent_task_finally_drops_the_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After enough confirmations spread out in time the report is retired."""
        monkeypatch.setattr(worker_cmd, "_GONE_HOLD_S", 0.0)
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-deleted", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-deleted/complete", _http(409))
        client.script(f"{SERVER}/tasks/t-deleted", _http(404, body={"detail": "Task 't-deleted' not found"}))

        for _ in range(worker_cmd._GONE_CONFIRMATIONS - 1):
            _retry_now(loop)
            loop._report_finished(client)
            assert _queued(loop), "an unconfirmed absence must keep the report owed"

        _retry_now(loop)
        loop._report_finished(client)

        assert _queued(loop) == []

    def test_a_proxy_404_never_drops_the_report(self, tmp_path: Path) -> None:
        """A 404 that does not even name the task says nothing about the task.

        A misrouted ingress answers this way for every path, so it must be held
        like a transport failure however often it repeats.
        """
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-proxy", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-proxy/complete", _http(409))
        client.script(f"{SERVER}/tasks/t-proxy", _http(404, body={"detail": "Not Found"}))

        for _ in range(worker_cmd._GONE_CONFIRMATIONS + 2):
            _retry_now(loop)
            loop._report_finished(client)

        (report,) = _queued(loop)
        assert report.absences == 0
        assert report.last_status.endswith("-unreconcilable")

    def test_422_reconciles_instead_of_blind_retry(self, tmp_path: Path) -> None:
        """422 means the slot was reclaimed and the task auto-failed."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-422", OUTCOME_FAILED, "boom")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-422/fail", _http(422, body={"error": "invalid transition"}))
        client.script(f"{SERVER}/tasks/t-422", _task("t-422", "failed"))

        loop._report_finished(client)

        assert len(client.posted("t-422", "fail")) == 1
        assert _queued(loop) == []

    def test_pending_approval_counts_as_the_reported_outcome(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-gate", OUTCOME_DONE, "done")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-gate/complete", _http(409))
        client.script(f"{SERVER}/tasks/t-gate", _task("t-gate", "pending_approval"))

        loop._report_finished(client)

        assert _queued(loop) == []


class TestUnknownExitIsNeverSuccess:
    def test_unknown_exit_fails_a_task_that_is_still_live(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-ghost", OUTCOME_UNKNOWN, "worker reaped agent process (exit status unavailable)")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-ghost", _task("t-ghost", "claimed"))
        client.script(f"{SERVER}/tasks/t-ghost/fail", _ack("t-ghost", "failed"))

        loop._report_finished(client)

        assert client.post_urls() == [f"{SERVER}/tasks/t-ghost/fail"]
        (body,) = client.posted("t-ghost", "fail")
        assert "could not observe the agent exit status" in body["reason"]
        assert "still claimed" in body["reason"]
        assert _queued(loop) == []

    def test_unknown_exit_settles_when_agent_already_self_completed(self, tmp_path: Path) -> None:
        """The common case: the agent finished its own task, then died."""
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-self", OUTCOME_UNKNOWN, "worker reaped agent process (exit status unavailable)")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-self", _task("t-self", "done"))

        loop._report_finished(client)

        assert client.post_calls == []
        assert _queued(loop) == []

    def test_unknown_exit_never_posts_complete(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._outbox.append("t-never", OUTCOME_UNKNOWN, "worker reaped agent process (exit status unavailable)")
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-never", httpx.ConnectError("central is down"))

        loop._report_finished(client)

        assert "/complete" not in "".join(client.post_urls())
        assert [r.task_id for r in _queued(loop)] == ["t-never"]


class TestDeliverySurvivesRetriesAndRestarts:
    def test_restart_redelivers_the_same_branch_and_sha_without_repushing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restart must not move HEAD out from under the janitor (#6538)."""
        first = _make_loop(tmp_path)
        _retry_now(first)
        first._active_tasks = {"task-9": 999}
        first._task_sessions["task-9"] = "sess-9"
        pushes: list[str] = []
        monkeypatch.setattr(first, "_push_agent_work", lambda tid: pushes.append(tid) or ("agent/sess-9", "aaa111"))
        _reap_with_exit_code(first, monkeypatch, 0, 0)

        outage = FakeCentral()
        outage.script(f"{SERVER}/tasks/task-9/complete", _http(503))
        first._report_finished(outage)
        assert len(_queued(first)) == 1

        # The node restarts: a fresh loop over the same workdir.
        second = _make_loop(tmp_path)
        monkeypatch.setattr(second, "_push_agent_work", lambda tid: pushes.append(tid) or ("agent/sess-9", "zzz999"))
        recovered = FakeCentral()
        recovered.script(f"{SERVER}/tasks/task-9/complete", _ack("task-9"))

        second._report_finished(recovered)

        assert pushes == ["task-9"]  # pushed exactly once, at reap time
        assert recovered.posted("task-9", "complete") == [
            {"result_summary": "agent exited with code 0", "agent_branch": "agent/sess-9", "agent_sha": "aaa111"}
        ]
        assert _queued(second) == []

    def test_report_written_before_shutdown_is_owed_after_restart(self, tmp_path: Path) -> None:
        """Shutdown with an unposted report loses nothing."""
        first = _make_loop(tmp_path)
        first._outbox.append("t-shutdown", OUTCOME_FAILED, "boom")

        second = _make_loop(tmp_path)
        client = FakeCentral()
        client.script(f"{SERVER}/tasks/t-shutdown/fail", _ack("t-shutdown", "failed"))
        second._report_finished(client)

        assert [url for url, _ in client.post_calls] == [f"{SERVER}/tasks/t-shutdown/fail"]
        assert _queued(second) == []

    def test_second_node_does_not_steal_another_nodes_report(self, tmp_path: Path) -> None:
        """Two workers sharing one .sdd/ must not double-post one outcome."""
        owner = _make_loop(tmp_path, name="node-a")
        owner._outbox.append("t-owned", OUTCOME_DONE, "done")
        peer = _make_loop(tmp_path, name="node-b")

        assert [r.task_id for r in peer._outbox.due()] == []
        assert [r.task_id for r in owner._outbox.due()] == ["t-owned"]
