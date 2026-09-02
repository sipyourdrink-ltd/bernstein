"""Adapter contract test: one synthetic transaction, many adapters.

Drives a throwaway entity with a deterministic, date-derived id through a
real :class:`~bernstein.core.trackers.contract.AbstractTrackerAdapter`
implementation, runs the shared ordered validator list against it, and
lets the last validator delete it and assert absence.

Two adapters are exercised here:

* :class:`~tests.fixtures.trackers.in_memory_tracker.InMemoryTracker` --
  the canonical reference implementation of the contract.
* :class:`~bernstein.core.trackers.builtin.gitlab_adapter.GitLabAdapter`
  -- a real HTTP adapter, driven against a stateful GitLab API double so
  the transport, auth headers, pagination and error mapping in the
  adapter all run for real without needing sandbox credentials.

The same ``DEFAULT_VALIDATORS`` list runs against both, and the
registry-driven test at the bottom asserts every registered adapter is
reachable by the same runner.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from bernstein.core.trackers.builtin.gitlab_adapter import (
    DEFAULT_GITLAB_URL,
    GITLAB_API_PATH,
    GitLabAdapter,
    GitLabConfig,
)
from bernstein.core.trackers.contract import (
    AbstractTrackerAdapter,
    CommentResult,
    Ticket,
    TrackerError,
    TransitionResult,
)
from bernstein.core.trackers.registry import get_registry
from bernstein.core.trackers.synthetic import (
    DEFAULT_VALIDATORS,
    PROBE_OPERATIONS,
    SyntheticProbeUnsupported,
    ValidatorSkipped,
    run_synthetic_transaction,
    synthetic_marker,
)
from tests.fixtures.trackers.in_memory_tracker import InMemoryTracker

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.trackers.synthetic import SyntheticContext

PROBE_DAY = dt.date(2026, 3, 4)
PROJECT = "my-group/my-project"


# ---------------------------------------------------------------------------
# GitLab API double
# ---------------------------------------------------------------------------


class _FakeGitLab:
    """Minimal stateful GitLab Issues API used by the respx transport.

    Implements only the six endpoints the synthetic transaction touches:
    create, list-open, search-by-title, note, label update, delete.
    """

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.notes: list[dict[str, Any]] = []
        self._next_iid = 1
        self.deleted: list[int] = []

    # -- routing ----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        prefix = f"{GITLAB_API_PATH}/projects/"
        if not path.startswith(prefix):  # pragma: no cover - defensive
            return httpx.Response(404, json={"message": "404 Not Found"})
        rest = path[len(prefix) :]
        # rest looks like "<project>/issues[/<iid>[/notes]]"; the project
        # segment may itself contain a slash once httpx decodes ``%2F``, so
        # split on the first "/issues" rather than the first "/".
        cut = rest.find("/issues")
        if cut == -1:  # pragma: no cover - defensive
            return httpx.Response(404, json={"message": "404 Not Found"})
        tail = rest[cut + 1 :]
        if method == "POST" and tail == "issues":
            return self._create(request)
        if method == "GET" and tail == "issues":
            return self._list(request)
        match = re.fullmatch(r"issues/(\d+)", tail)
        if match is not None:
            iid = int(match.group(1))
            if method == "PUT":
                return self._update(iid, request)
            if method == "DELETE":
                return self._delete(iid)
            if method == "GET":
                return self._get(iid)
        match = re.fullmatch(r"issues/(\d+)/notes", tail)
        if match is not None and method == "POST":
            return self._note(int(match.group(1)), request)
        return httpx.Response(404, json={"message": "404 Not Found"})  # pragma: no cover

    def seed_issue(self, title: str) -> int:
        """Insert an unrelated, operator-owned issue and return its iid."""
        iid = self._next_iid
        self._next_iid += 1
        self.issues[iid] = {
            "id": 1000 + iid,
            "iid": iid,
            "project_id": 7,
            "title": title,
            "description": "",
            "labels": [],
            "state": "opened",
            "web_url": f"https://gitlab.com/{PROJECT}/-/issues/{iid}",
            "assignee": None,
        }
        return iid

    # -- endpoints --------------------------------------------------------

    def _create(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        iid = self._next_iid
        self._next_iid += 1
        issue = {
            "id": 1000 + iid,
            "iid": iid,
            "project_id": 7,
            "title": str(payload.get("title") or ""),
            "description": str(payload.get("description") or ""),
            "labels": [lab for lab in str(payload.get("labels") or "").split(",") if lab],
            "state": "opened",
            "web_url": f"https://gitlab.com/{PROJECT}/-/issues/{iid}",
            "assignee": None,
        }
        self.issues[iid] = issue
        return httpx.Response(201, json=issue)

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        state = params.get("state")
        search = params.get("search")
        page = int(params.get("page") or 1)
        rows = list(self.issues.values())
        if state and state != "all":
            rows = [r for r in rows if r["state"] == state]
        if search:
            field = "title" if params.get("in") == "title" else "description"
            rows = [r for r in rows if search in r[field]]
        # Single-page responses are enough for the probe; page 2+ is empty
        # so the adapter's pagination loop terminates.
        if page > 1:
            rows = []
        return httpx.Response(200, json=rows)

    def _get(self, iid: int) -> httpx.Response:
        issue = self.issues.get(iid)
        if issue is None:
            return httpx.Response(404, json={"message": "404 Issue Not Found"})
        return httpx.Response(200, json=issue)

    def _update(self, iid: int, request: httpx.Request) -> httpx.Response:
        issue = self.issues.get(iid)
        if issue is None:  # pragma: no cover - defensive
            return httpx.Response(404, json={"message": "404 Issue Not Found"})
        payload = json.loads(request.content or b"{}")
        labels = set(issue["labels"])
        for lab in str(payload.get("remove_labels") or "").split(","):
            labels.discard(lab)
        for lab in str(payload.get("add_labels") or "").split(","):
            if lab:
                labels.add(lab)
        issue["labels"] = sorted(labels)
        return httpx.Response(200, json=issue)

    def _delete(self, iid: int) -> httpx.Response:
        if iid not in self.issues:
            return httpx.Response(404, json={"message": "404 Issue Not Found"})
        del self.issues[iid]
        self.deleted.append(iid)
        return httpx.Response(204)

    def _note(self, iid: int, request: httpx.Request) -> httpx.Response:
        if iid not in self.issues:  # pragma: no cover - defensive
            return httpx.Response(404, json={"message": "404 Issue Not Found"})
        payload = json.loads(request.content or b"{}")
        note = {
            "id": 500 + len(self.notes),
            "body": str(payload.get("body") or ""),
            "issue_iid": iid,
            "idempotency_key": request.headers.get("Idempotency-Key"),
        }
        self.notes.append(note)
        return httpx.Response(201, json=note)


@pytest.fixture
def gitlab_probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[GitLabAdapter, _FakeGitLab]]:
    """Yield a real ``GitLabAdapter`` wired to a stateful API double."""
    monkeypatch.setenv("GITLAB_PROBE_TOKEN", "glpat-test")
    fake = _FakeGitLab()
    with respx.mock(base_url=DEFAULT_GITLAB_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        adapter = GitLabAdapter(
            config=GitLabConfig(
                project_id_or_path=PROJECT,
                token_env="GITLAB_PROBE_TOKEN",
            ),
        )
        try:
            yield adapter, fake
        finally:
            adapter.close()


# ---------------------------------------------------------------------------
# 1. Deterministic id + create/delete round trip (load-bearing)
# ---------------------------------------------------------------------------


def test_synthetic_transaction_creates_and_deletes_with_deterministic_id(
    gitlab_probe: tuple[GitLabAdapter, _FakeGitLab],
) -> None:
    """A real adapter creates, drives, and deletes a dated throwaway entity."""
    adapter, fake = gitlab_probe
    marker = synthetic_marker("gitlab", day=PROBE_DAY)

    assert marker == "bernstein-synthetic-gitlab-20260304"
    assert synthetic_marker("gitlab", day=PROBE_DAY) == marker

    report = run_synthetic_transaction(adapter, day=PROBE_DAY, stream=io.StringIO())

    assert report.marker == marker
    assert report.ticket_id is not None
    assert report.exit_code == 0, report.render_lines()
    # The entity really existed on the tracker side and is gone afterwards.
    assert fake.deleted == [int(report.ticket_id)]
    assert fake.issues == {}
    assert adapter.find_probe_tickets(marker) == ()
    # The last validator is the one that deleted it.
    assert report.verdicts[-1].name == "delete_removes_the_synthetic_entity"
    assert report.verdicts[-1].outcome == "passed"


def test_synthetic_transaction_round_trips_the_in_memory_reference() -> None:
    """The same runner drives the canonical reference implementation."""
    adapter = InMemoryTracker()
    report = run_synthetic_transaction(adapter, day=PROBE_DAY, stream=io.StringIO())

    assert report.marker == "bernstein-synthetic-in_memory-20260304"
    assert report.exit_code == 0, report.render_lines()
    assert adapter.find_probe_tickets(report.marker) == ()


# ---------------------------------------------------------------------------
# 2. Re-runs collide with their own leftovers
# ---------------------------------------------------------------------------


def test_rerun_collides_with_own_leftover_and_cleans_it() -> None:
    """A leftover from an aborted run on the same day is swept, not duplicated."""
    adapter = InMemoryTracker()
    marker = synthetic_marker(adapter.name, day=PROBE_DAY)
    leftover = adapter.create_probe_ticket(marker)
    assert adapter.find_probe_tickets(marker) == (leftover,)

    report = run_synthetic_transaction(adapter, day=PROBE_DAY, stream=io.StringIO())

    assert report.leftovers_cleaned == 1
    assert report.ticket_id != leftover
    assert report.exit_code == 0, report.render_lines()
    # Nothing leaks: neither the leftover nor this run's entity survives.
    assert adapter.find_probe_tickets(marker) == ()


def test_rerun_on_the_same_day_leaves_nothing_behind() -> None:
    """Two consecutive runs on the same day end with an empty tracker."""
    adapter = InMemoryTracker()
    marker = synthetic_marker(adapter.name, day=PROBE_DAY)

    first = run_synthetic_transaction(adapter, day=PROBE_DAY, stream=io.StringIO())
    second = run_synthetic_transaction(adapter, day=PROBE_DAY, stream=io.StringIO())

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert second.leftovers_cleaned == 0
    assert adapter.find_probe_tickets(marker) == ()


# ---------------------------------------------------------------------------
# 3./4. Runner contract: exit code and printed verdicts
# ---------------------------------------------------------------------------


def _always_fails(ctx: SyntheticContext) -> None:
    """Validator that always fails; used to probe the runner's exit code."""
    msg = f"deliberate failure on {ctx.ticket_id}"
    raise AssertionError(msg)


def _always_skips(ctx: SyntheticContext) -> None:
    """Validator that always records a skip."""
    del ctx
    raise ValidatorSkipped("not applicable here")


def test_validator_runner_exits_nonzero_on_any_failure() -> None:
    """One failing validator makes the whole run non-zero, and cleanup still runs."""
    adapter = InMemoryTracker()
    validators = (_always_skips, _always_fails, *DEFAULT_VALIDATORS)

    report = run_synthetic_transaction(
        adapter,
        validators=validators,
        day=PROBE_DAY,
        stream=io.StringIO(),
    )

    assert report.exit_code == 1
    assert not report.ok
    outcomes = {v.name: v.outcome for v in report.verdicts}
    assert outcomes["_always_skips"] == "skipped"
    assert outcomes["_always_fails"] == "failed"
    # A skip is not a failure.
    assert [v.name for v in report.failures] == ["_always_fails"]
    # The failure did not abort the run: the deleting validator still ran.
    assert outcomes["delete_removes_the_synthetic_entity"] == "passed"
    assert adapter.find_probe_tickets(report.marker) == ()


def test_validator_runner_records_the_error_taxonomy_of_a_failure() -> None:
    """A failing validator's verdict names the tracker error class, not just 'failed'."""
    adapter = InMemoryTracker()

    def _raises_tracker_error(ctx: SyntheticContext) -> None:
        del ctx
        msg = "upstream said no"
        raise TrackerError(msg)

    report = run_synthetic_transaction(
        adapter,
        validators=(_raises_tracker_error, *DEFAULT_VALIDATORS),
        day=PROBE_DAY,
        stream=io.StringIO(),
    )

    verdict = next(v for v in report.verdicts if v.name == "_raises_tracker_error")
    assert verdict.error_kind == "TrackerError"
    assert "upstream said no" in verdict.detail


def test_validator_runner_prints_each_validators_verdict() -> None:
    """Every validator, in order, gets exactly one printed verdict line."""
    adapter = InMemoryTracker()
    stream = io.StringIO()
    validators = (_always_skips, *DEFAULT_VALIDATORS)

    report = run_synthetic_transaction(
        adapter,
        validators=validators,
        day=PROBE_DAY,
        stream=stream,
    )

    printed = stream.getvalue().splitlines()
    verdict_lines = [line for line in printed if line.startswith(("PASS", "FAIL", "SKIP"))]
    assert len(verdict_lines) == len(validators)
    assert [line.split()[1] for line in verdict_lines] == [v.__name__ for v in validators]
    assert verdict_lines[0].startswith("SKIP")
    assert report.render_lines() == printed


# ---------------------------------------------------------------------------
# 5. The same list runs against every registered adapter
# ---------------------------------------------------------------------------


def _builtin_names() -> list[str]:
    """Registered built-in adapter names, read from the registry itself."""
    return [entry.name for entry in get_registry() if entry.source == "builtin"]


class _ProbelessAdapter(AbstractTrackerAdapter):
    """A contract-complete adapter that cannot host a throwaway entity."""

    def __init__(self, name: str) -> None:
        self.name = name

    def pull_open_tickets(self, filter: dict[str, Any] | None = None) -> Iterator[Ticket]:
        del filter
        return iter(())

    def add_comment(self, ticket_id: str, body: str, *, idempotency_key: str | None = None) -> CommentResult:
        del body, idempotency_key
        return CommentResult(comment_id="c", ticket_id=ticket_id)

    def transition(
        self,
        ticket_id: str,
        status_id: str,
        *,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> TransitionResult:
        del idempotency_key, etag
        return TransitionResult(ticket_id=ticket_id, new_status=status_id)


@pytest.mark.parametrize("tracker_name", _builtin_names())
def test_same_validator_list_runs_against_every_builtin_adapter(
    tracker_name: str,
    gitlab_probe: tuple[GitLabAdapter, _FakeGitLab],
) -> None:
    """One validator list covers every registered adapter, with no per-adapter branch.

    An adapter that declares the probe surface is driven through the full
    ``DEFAULT_VALIDATORS`` list; one that does not is refused by name with
    :class:`SyntheticProbeUnsupported`. A newly registered adapter joins
    this parametrization automatically instead of needing its own file.
    """
    entry = get_registry().get(tracker_name)
    supports_probe = all(hasattr(entry.factory, op) for op in PROBE_OPERATIONS)

    if supports_probe:
        # GitLab is the reference probe implementation for this slice.
        assert tracker_name == "gitlab"
        adapter, _fake = gitlab_probe
        report = run_synthetic_transaction(adapter, day=PROBE_DAY, stream=io.StringIO())
        assert [v.name for v in report.verdicts] == [v.__name__ for v in DEFAULT_VALIDATORS]
        assert report.exit_code == 0, report.render_lines()
        return

    with pytest.raises(SyntheticProbeUnsupported) as excinfo:
        run_synthetic_transaction(_ProbelessAdapter(tracker_name), day=PROBE_DAY, stream=io.StringIO())
    assert tracker_name in str(excinfo.value)


def test_at_least_one_builtin_adapter_declares_the_probe_surface() -> None:
    """Guards the parametrized test above against silently covering nothing."""
    supported = [
        entry.name
        for entry in get_registry()
        if entry.source == "builtin" and all(hasattr(entry.factory, op) for op in PROBE_OPERATIONS)
    ]
    assert supported == ["gitlab"]


# ---------------------------------------------------------------------------
# Destructive-operation guard
# ---------------------------------------------------------------------------


def test_delete_probe_ticket_refuses_a_ticket_without_the_synthetic_marker(
    gitlab_probe: tuple[GitLabAdapter, _FakeGitLab],
) -> None:
    """``delete_probe_ticket`` never deletes a ticket it did not create."""
    adapter, fake = gitlab_probe
    iid = fake.seed_issue("A real operator ticket")

    with pytest.raises(TrackerError, match="does not carry the synthetic marker"):
        adapter.delete_probe_ticket(str(iid), synthetic_marker("gitlab", day=PROBE_DAY))

    assert fake.deleted == []
    assert iid in fake.issues


def test_in_memory_delete_probe_ticket_refuses_an_unmarked_ticket() -> None:
    """The reference implementation carries the same guard."""
    adapter = InMemoryTracker()
    ticket = adapter.seed("A real operator ticket")

    with pytest.raises(TrackerError, match="does not carry the synthetic marker"):
        adapter.delete_probe_ticket(ticket.id, synthetic_marker("in_memory", day=PROBE_DAY))
