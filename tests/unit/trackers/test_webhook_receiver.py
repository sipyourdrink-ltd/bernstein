"""Tests for :mod:`bernstein.core.trackers.webhook_receiver`."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.trackers import Ticket
from bernstein.core.trackers.contract import RateLimited, TrackerUnavailable
from bernstein.core.trackers.webhook_receiver import (
    PollWatermarks,
    ReceiveResult,
    ReplayLedger,
    TicketUpsertSink,
    TrackerEvent,
    WebhookConfig,
    WebhookHandler,
    WebhookReceiver,
    get_handler,
    list_handlers,
    register_builtin_handlers,
    register_handler,
    replay_recent_via_poll,
    replay_recent_via_poll_all,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _prefixed_sig(secret: str, body: bytes) -> str:
    return "sha256=" + _hex_sig(secret, body)


def _make_receiver(adapter: str, *, secret_env: str = "TEST_WH_SECRET") -> WebhookReceiver:
    receiver = WebhookReceiver()
    receiver.configure(adapter, WebhookConfig(enabled=True, secret_env=secret_env))
    return receiver


# ---------------------------------------------------------------------------
# ReplayLedger
# ---------------------------------------------------------------------------


def test_replay_ledger_dedupes_in_memory() -> None:
    ledger = ReplayLedger(max_entries=4)
    assert ledger.remember("a") is True
    assert ledger.remember("a") is False
    assert ledger.seen("a") is True
    assert ledger.seen("b") is False


def test_replay_ledger_persists_to_disk(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    ledger1 = ReplayLedger(p)
    ledger1.remember("d-1")
    ledger1.remember("d-2")
    # Construct a second ledger pointing at the same file - replay should be
    # rejected even after a "restart".
    ledger2 = ReplayLedger(p)
    assert ledger2.seen("d-1") is True
    assert ledger2.remember("d-1") is False
    assert ledger2.remember("d-3") is True


def test_replay_ledger_evicts_oldest() -> None:
    ledger = ReplayLedger(max_entries=2)
    ledger.remember("x")
    ledger.remember("y")
    ledger.remember("z")
    # ``x`` should have been evicted now that ``z`` is in.
    assert ledger.seen("x") is False
    assert ledger.seen("y") is True
    assert ledger.seen("z") is True


def test_replay_ledger_disk_failure_does_not_raise(tmp_path: Path) -> None:
    # Use a path under a non-writable directory.  We simulate by pointing
    # at a child of a file (cannot be a directory).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bad_path = blocker / "ledger.jsonl"
    ledger = ReplayLedger(bad_path)
    # remember() must not raise even though mkdir + open will fail.
    assert ledger.remember("only-in-memory") is True
    assert ledger.seen("only-in-memory") is True


# ---------------------------------------------------------------------------
# Built-in handler registration
# ---------------------------------------------------------------------------


def test_builtin_handlers_registered() -> None:
    register_builtin_handlers()
    names = set(list_handlers())
    assert {"jira_cloud", "github", "gitlab", "linear", "plane"} <= names


# ---------------------------------------------------------------------------
# Receiver - verification & disabled paths
# ---------------------------------------------------------------------------


def test_receive_disabled_returns_disabled() -> None:
    receiver = WebhookReceiver()
    # No configure() call - adapter is disabled.
    result = receiver.receive("github", {}, b"{}")
    assert result.status == "disabled"


def test_receive_unknown_adapter() -> None:
    receiver = _make_receiver("nope")
    result = receiver.receive("nope", {}, b"{}")
    assert result.status == "unknown_adapter"


def test_receive_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_WH_SECRET", raising=False)
    receiver = _make_receiver("github")
    result = receiver.receive("github", {}, b"{}")
    assert result.status == "not_configured"


def test_receive_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    receiver = _make_receiver("github")
    result = receiver.receive(
        "github",
        {"x-hub-signature-256": "sha256=" + "0" * 64},
        b"{}",
    )
    assert result.status == "bad_signature"


# ---------------------------------------------------------------------------
# GitHub handler
# ---------------------------------------------------------------------------


def _github_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "issue": {
            "id": 1,
            "number": 42,
            "html_url": "https://github.com/acme/repo/issues/42",
            "title": "Bug: parser crash",
            "body": "stack trace",
            "state": "open",
            "labels": [{"name": "bug"}, {"name": "p1"}],
        },
        "repository": {"full_name": "acme/repo"},
    }


def test_github_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    receiver = _make_receiver("github")
    body = json.dumps(_github_payload()).encode("utf-8")
    headers = {
        "x-hub-signature-256": _prefixed_sig("shh", body),
        "x-github-event": "issues",
        "x-github-delivery": "deadbeef-1",
    }
    result = receiver.receive("github", headers, body)
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.adapter == "github"
    assert result.event.ticket.id == "acme/repo#42"
    assert result.event.ticket.labels == ("bug", "p1")
    assert result.event.delivery_id == "github:deadbeef-1"


def test_github_replay_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    receiver = _make_receiver("github")
    body = json.dumps(_github_payload()).encode("utf-8")
    headers = {
        "x-hub-signature-256": _prefixed_sig("shh", body),
        "x-github-event": "issues",
        "x-github-delivery": "abc-replay",
    }
    first = receiver.receive("github", headers, body)
    second = receiver.receive("github", headers, body)
    assert first.status == "accepted"
    assert second.status == "replay"


def test_github_bad_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    receiver = _make_receiver("github")
    body = b"not json"
    headers = {
        "x-hub-signature-256": _prefixed_sig("shh", body),
        "x-github-event": "issues",
        "x-github-delivery": "bad-1",
    }
    result = receiver.receive("github", headers, body)
    assert result.status == "bad_payload"


def test_github_ignores_unhandled_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    receiver = _make_receiver("github")
    body = json.dumps({"action": "completed"}).encode("utf-8")
    headers = {
        "x-hub-signature-256": _prefixed_sig("shh", body),
        "x-github-event": "workflow_run",
        "x-github-delivery": "skip-1",
    }
    result = receiver.receive("github", headers, body)
    assert result.status == "ignored"


# ---------------------------------------------------------------------------
# Jira Cloud handler
# ---------------------------------------------------------------------------


def test_jira_cloud_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "jira-shh")
    receiver = _make_receiver("jira_cloud")
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "id": "10042",
            "key": "ACME-7",
            "self": "https://acme.atlassian.net/rest/api/3/issue/10042",
            "fields": {
                "summary": "Refactor parser",
                "description": "details",
                "status": {"name": "In Progress"},
                "labels": ["backend", "p2"],
            },
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-hub-signature-256": _prefixed_sig("jira-shh", body),
        "x-atlassian-webhook-identifier": "jira-1",
    }
    result = receiver.receive("jira_cloud", headers, body)
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.ticket.id == "ACME-7"
    assert result.event.ticket.external_url.startswith("https://acme.atlassian.net/browse/ACME-7")
    assert result.event.ticket.status == "In Progress"
    assert result.event.ticket.labels == ("backend", "p2")
    assert result.event.delivery_id == "jira_cloud:jira-1"


def test_jira_cloud_missing_issue_returns_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "jira-shh")
    receiver = _make_receiver("jira_cloud")
    body = json.dumps({"webhookEvent": "noop"}).encode("utf-8")
    headers = {
        "x-hub-signature-256": _prefixed_sig("jira-shh", body),
        "x-atlassian-webhook-identifier": "jira-empty",
    }
    result = receiver.receive("jira_cloud", headers, body)
    assert result.status == "ignored"


# ---------------------------------------------------------------------------
# GitLab handler
# ---------------------------------------------------------------------------


def test_gitlab_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "gl-token")
    receiver = _make_receiver("gitlab")
    payload = {
        "object_kind": "issue",
        "object_attributes": {
            "iid": 17,
            "title": "Race condition",
            "description": "see logs",
            "state": "opened",
            "url": "https://gitlab.example.com/acme/repo/-/issues/17",
            "action": "open",
        },
        "project": {"path_with_namespace": "acme/repo"},
        "labels": [{"title": "bug"}],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-gitlab-token": "gl-token",
        "x-gitlab-event-uuid": "gl-1",
    }
    result = receiver.receive("gitlab", headers, body)
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.ticket.id == "acme/repo#17"
    assert result.event.ticket.labels == ("bug",)
    assert result.event.delivery_id == "gitlab:gl-1"


def test_gitlab_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "gl-token")
    receiver = _make_receiver("gitlab")
    body = json.dumps({"object_kind": "issue"}).encode("utf-8")
    headers = {"x-gitlab-token": "wrong"}
    result = receiver.receive("gitlab", headers, body)
    assert result.status == "bad_signature"


# ---------------------------------------------------------------------------
# Linear handler
# ---------------------------------------------------------------------------


def test_linear_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "ln-secret")
    receiver = _make_receiver("linear")
    payload = {
        "type": "Issue",
        "action": "update",
        "data": {
            "id": "abc-uuid",
            "identifier": "ENG-12",
            "title": "Investigate flake",
            "description": "happens twice a week",
            "state": {"name": "In Progress"},
            "url": "https://linear.app/acme/issue/ENG-12",
            "labels": {"nodes": [{"name": "flake"}]},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "linear-signature": _hex_sig("ln-secret", body),
        "linear-delivery": "ln-1",
    }
    result = receiver.receive("linear", headers, body)
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.ticket.id == "ENG-12"
    assert result.event.ticket.labels == ("flake",)
    assert result.event.delivery_id == "linear:ln-1"


# ---------------------------------------------------------------------------
# Plane handler
# ---------------------------------------------------------------------------


def test_plane_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "plane-secret")
    receiver = _make_receiver("plane")
    payload = {
        "event": "issue.updated",
        "action": "updated",
        "data": {
            "id": "issue-uuid",
            "sequence_id": 3,
            "project": "proj-uuid",
            "name": "Telemetry stuck",
            "description_stripped": "details",
            "state": "In Progress",
            "url": "https://plane.example.com/.../issues/issue-uuid",
            "labels": ["telemetry"],
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-plane-signature": _hex_sig("plane-secret", body),
        "x-plane-delivery": "plane-1",
    }
    result = receiver.receive("plane", headers, body)
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.ticket.id == "proj-uuid#3"
    assert result.event.ticket.status == "In Progress"
    assert result.event.delivery_id == "plane:plane-1"


# ---------------------------------------------------------------------------
# Custom handler registration
# ---------------------------------------------------------------------------


def test_register_custom_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    def _verify(headers: dict[str, str], body: bytes, secret: str) -> bool:
        del body
        return headers.get("x-test-token") == secret

    def _parse(headers: dict[str, str], payload: dict[str, Any]) -> TrackerEvent | None:
        del headers
        if not payload.get("id"):
            return None
        return TrackerEvent(
            adapter="custom",
            action=str(payload.get("action") or "updated"),
            ticket=Ticket(
                id=str(payload["id"]),
                external_url="",
                title=str(payload.get("title") or ""),
                body="",
                status="open",
            ),
            delivery_id="",
        )

    def _delivery(headers: dict[str, str], body: bytes) -> str:
        del body
        return f"custom:{headers.get('x-test-delivery', 'unknown')}"

    register_handler(
        WebhookHandler(
            adapter="custom_test_tracker",
            verify_signature=_verify,
            parse_event=_parse,
            delivery_id=_delivery,
        )
    )
    assert get_handler("custom_test_tracker") is not None

    monkeypatch.setenv("CUSTOM_WH", "abc")
    receiver = _make_receiver("custom_test_tracker", secret_env="CUSTOM_WH")
    headers = {"x-test-token": "abc", "x-test-delivery": "d-1"}
    body = json.dumps({"id": "t-1", "action": "created"}).encode("utf-8")
    result = receiver.receive("custom_test_tracker", headers, body)
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.delivery_id == "custom:d-1"


# ---------------------------------------------------------------------------
# Startup-poll recovery
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, tickets: list[Ticket]) -> None:
        self._tickets = tickets

    def pull_open_tickets(self) -> Any:
        return iter(self._tickets)


def test_replay_recent_via_poll_filters_by_timestamp() -> None:
    old = Ticket(
        id="OLD-1",
        external_url="",
        title="old",
        body="",
        status="open",
        raw={"updated_at": 100.0},
    )
    fresh = Ticket(
        id="NEW-1",
        external_url="",
        title="fresh",
        body="",
        status="open",
        raw={"updated_at": 999.0},
    )
    delivered: list[Ticket] = []
    n = replay_recent_via_poll(
        _FakeAdapter([old, fresh]),
        last_processed_ts=500.0,
        sink=delivered.append,
    )
    assert n == 1
    assert delivered[0].id == "NEW-1"


def test_replay_recent_via_poll_no_timestamps_replays_all() -> None:
    tickets = [
        Ticket(id="A", external_url="", title="a", body="", status="open"),
        Ticket(id="B", external_url="", title="b", body="", status="open"),
    ]
    delivered: list[Ticket] = []
    n = replay_recent_via_poll(_FakeAdapter(tickets), last_processed_ts=0.0, sink=delivered.append)
    assert n == 2


def test_replay_recent_via_poll_adapter_without_pull_returns_zero() -> None:
    class _NoPull:
        pass

    n = replay_recent_via_poll(_NoPull(), last_processed_ts=0.0, sink=lambda t: None)
    assert n == 0


# ---------------------------------------------------------------------------
# Persisted watermark, early exit, isolation, deadline, backoff, upsert
# ---------------------------------------------------------------------------


def _ticket(ident: str, ts: float | None = None, *, title: str = "t") -> Ticket:
    raw: dict[str, Any] = {} if ts is None else {"updated_at": ts}
    return Ticket(
        id=ident,
        external_url="",
        title=title,
        body="",
        status="open",
        raw=raw,
    )


class _RecordingAdapter:
    """Adapter fixture that records which tickets each poll actually yielded."""

    def __init__(self, tickets: list[Ticket], *, newest_first: bool = False) -> None:
        self._tickets = list(tickets)
        if newest_first:
            self._tickets.sort(
                key=lambda t: float(t.raw.get("updated_at", 0.0)),
                reverse=True,
            )
        self.yielded: list[str] = []

    def pull_open_tickets(self) -> Any:
        for ticket in self._tickets:
            self.yielded.append(ticket.id)
            yield ticket


class _Clock:
    """Manually advanced clock so deadline tests stay deterministic."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def test_two_consecutive_polls_over_unchanged_fixture_fetch_zero_new_records_second_time(
    tmp_path: Path,
) -> None:
    marks = PollWatermarks(tmp_path / "watermarks.jsonl")
    fixture = [_ticket("A", 100.0), _ticket("B", 200.0)]
    sink = TicketUpsertSink()

    first = replay_recent_via_poll(_RecordingAdapter(fixture), sink=sink, source="jira", watermarks=marks)
    assert first == 2

    second = replay_recent_via_poll(_RecordingAdapter(fixture), sink=sink, source="jira", watermarks=marks)
    assert second == 0
    assert len(sink) == 2


def test_watermark_persists_across_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "watermarks.jsonl"
    replay_recent_via_poll(
        _RecordingAdapter([_ticket("A", 100.0)]),
        sink=lambda t: None,
        source="jira",
        watermarks=PollWatermarks(path),
    )

    reloaded = PollWatermarks(path)
    assert reloaded.get("jira") == 100.0

    delivered: list[Ticket] = []
    n = replay_recent_via_poll(
        _RecordingAdapter([_ticket("A", 100.0)]),
        sink=delivered.append,
        source="jira",
        watermarks=reloaded,
    )
    assert n == 0
    assert delivered == []


def test_newest_first_stops_at_first_record_older_than_watermark(tmp_path: Path) -> None:
    marks = PollWatermarks(tmp_path / "watermarks.jsonl")
    marks.advance("jira", 150.0)
    adapter = _RecordingAdapter(
        [
            _ticket("A", 100.0),
            _ticket("B", 120.0),
            _ticket("C", 200.0),
            _ticket("D", 300.0),
        ],
        newest_first=True,
    )

    delivered: list[Ticket] = []
    n = replay_recent_via_poll(
        adapter,
        sink=delivered.append,
        source="jira",
        watermarks=marks,
        newest_first=True,
    )

    assert n == 2
    assert [t.id for t in delivered] == ["D", "C"]
    # The scan stops at the first record at or below the watermark; "A" is
    # never pulled from the adapter at all.
    assert adapter.yielded == ["D", "C", "B"]
    assert marks.get("jira") == 300.0


def test_one_resource_type_error_does_not_abort_the_others(tmp_path: Path) -> None:
    class _Broken:
        def pull_open_tickets(self) -> Any:
            raise TrackerUnavailable("issues endpoint returned 503")

    delivered: list[Ticket] = []
    result = replay_recent_via_poll_all(
        {"issues": _Broken(), "epics": _RecordingAdapter([_ticket("E-1", 10.0)])},
        sink=delivered.append,
        watermarks=PollWatermarks(tmp_path / "watermarks.jsonl"),
    )

    assert result.delivered["epics"] == 1
    assert result.delivered["issues"] == 0
    assert "issues" in result.errors
    assert "TrackerUnavailable" in result.errors["issues"]
    assert "epics" not in result.errors
    assert [t.id for t in delivered] == ["E-1"]


def test_poll_respects_wall_clock_bound(tmp_path: Path) -> None:
    clock = _Clock()

    class _SlowAdapter:
        def __init__(self, tickets: list[Ticket], step: float) -> None:
            self._tickets = tickets
            self._step = step

        def pull_open_tickets(self) -> Any:
            for ticket in self._tickets:
                clock.advance(self._step)
                yield ticket

    marks = PollWatermarks(tmp_path / "watermarks.jsonl")
    slow = _SlowAdapter([_ticket("S-1", 10.0), _ticket("S-2", 20.0), _ticket("S-3", 30.0)], 6.0)
    delivered: list[Ticket] = []

    result = replay_recent_via_poll_all(
        {"slow": slow, "second": _RecordingAdapter([_ticket("X-1", 1.0)])},
        sink=delivered.append,
        watermarks=marks,
        deadline_s=10.0,
        now=clock,
    )

    assert result.delivered["slow"] == 1
    assert result.delivered["second"] == 0
    assert result.timed_out == ("slow", "second")
    assert result.errors == {}
    # A poll the deadline cut short must not advance the watermark, or the
    # records it never reached would be skipped forever.
    assert marks.get("slow") == 0.0


def test_rate_limited_response_backs_off_with_jitter_and_cap() -> None:
    class _LimitedAdapter:
        def __init__(self, failures: int) -> None:
            self.calls = 0
            self._failures = failures

        def pull_open_tickets(self) -> Any:
            self.calls += 1
            if self.calls <= self._failures:
                raise RateLimited("secondary rate limit", retry_after=600.0)
            return iter([_ticket("A", 10.0)])

    high_jitter: list[float] = []
    adapter = _LimitedAdapter(3)
    delivered: list[Ticket] = []
    n = replay_recent_via_poll(
        adapter,
        sink=delivered.append,
        max_attempts=4,
        backoff_cap_s=5.0,
        sleep=high_jitter.append,
        jitter=lambda: 1.0,
    )
    assert n == 1
    assert adapter.calls == 4
    assert [t.id for t in delivered] == ["A"]

    low_jitter: list[float] = []
    replay_recent_via_poll(
        _LimitedAdapter(3),
        sink=lambda t: None,
        max_attempts=4,
        backoff_cap_s=5.0,
        sleep=low_jitter.append,
        jitter=lambda: 0.0,
    )

    # ``retry_after`` of 600s is clamped to the 5s cap, and the jitter term
    # moves the wait within the cap rather than beyond it.
    assert high_jitter == [5.0, 5.0, 5.0]
    assert low_jitter == [2.5, 2.5, 2.5]
    assert all(0.0 < wait <= 5.0 for wait in high_jitter + low_jitter)


def test_rate_limit_gives_up_after_max_attempts() -> None:
    class _AlwaysLimited:
        def pull_open_tickets(self) -> Any:
            raise RateLimited("still limited", retry_after=1.0)

    with pytest.raises(RateLimited):
        replay_recent_via_poll(
            _AlwaysLimited(),
            sink=lambda t: None,
            max_attempts=2,
            sleep=lambda _s: None,
            jitter=lambda: 0.0,
        )


def test_sink_upserts_on_stable_id_not_duplicate_inserts(tmp_path: Path) -> None:
    sink = TicketUpsertSink()
    sink(_ticket("A", 100.0, title="first"))
    sink(_ticket("A", 200.0, title="second"))
    sink(_ticket("B", 100.0, title="other"))

    assert len(sink) == 2
    assert {t.id for t in sink.tickets} == {"A", "B"}
    assert next(t for t in sink.tickets if t.id == "A").title == "second"

    # A retried poll re-delivers the same ids; the far side keeps one record
    # per stable id instead of appending duplicates.
    marks = PollWatermarks(tmp_path / "watermarks.jsonl")
    fixture = [_ticket("A", 300.0, title="third"), _ticket("C", 300.0)]
    replay_recent_via_poll(_RecordingAdapter(fixture), sink=sink, source="s", watermarks=marks)
    replay_recent_via_poll(_RecordingAdapter(fixture), sink=sink, source="s", watermarks=None)

    assert len(sink) == 3
    assert next(t for t in sink.tickets if t.id == "A").title == "third"


# ---------------------------------------------------------------------------
# Receive result helper
# ---------------------------------------------------------------------------


def test_receive_result_defaults() -> None:
    r = ReceiveResult(status="accepted")
    assert r.delivery_id is None
    assert r.event is None
