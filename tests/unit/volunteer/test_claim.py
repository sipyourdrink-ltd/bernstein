"""Claim etiquette is a decision about a GitHub thread, so these tests fake the
thread rather than the clock or the code under test.

The ``gh`` CLI is the only outside edge, and it is stubbed by a runner that
routes argument vectors to in-memory state -- no subprocess, no network.  The
clock is injected, so "fresh" and "stale" are exact rather than timing-dependent.
Everything else is the real decision logic: the same ``should_skip`` and
``ClaimClient`` a donor's machine runs.

The property that carries the acceptance criterion is per-donor scoping.  A claim
comment written by another donor must never be edited (GitHub answers a
cross-author edit with 403), and a worker's own fresh claim must not lock it out
of its own task.  Both come from ``viewerDidAuthor``, and both are asserted here
against a thread that mixes authors.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.volunteer.claim import (
    CLAIM_MARKER,
    DEFAULT_CLAIM_STALENESS,
    RESOLVED_MARKER,
    ClaimClient,
    build_claim_body,
    find_own_claim,
    repo_slug,
    resolve_fingerprint,
    should_skip,
)
from bernstein.core.volunteer.runner import (
    AgentInvocation,
    ClaimedTask,
    DonorLimits,
    RefusalStage,
    TaskRefusal,
    run_claimed_task,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _no_agent(invocation: AgentInvocation) -> Sequence[str]:
    """A launcher neither test reaches; both refuse before the agent runs."""
    return []


#: A fixed "now" so freshness is exact.  Comments are dated relative to it.
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

#: Repository slug the fake thread lives under.  The fake runner ignores it; it
#: only has to be consistent.
REPO = "acme/widgets"
NUMBER = 3873


# ---------------------------------------------------------------------------
# Fake ``gh`` runner and thread builders
# ---------------------------------------------------------------------------


def _iso(when: datetime) -> str:
    """A gh-style ISO-8601 timestamp with a trailing ``Z``."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _comment(
    comment_id: int, *, mine: bool, age: timedelta, marker: bool = True, resolved: bool = False
) -> dict[str, Any]:
    """One issue comment as ``gh issue view --json comments`` renders it."""
    if not marker:
        body = "just a passer-by comment"
    elif resolved:
        body = f"{CLAIM_MARKER}\n{RESOLVED_MARKER}\nCompleted via bernstein."
    else:
        body = f"{CLAIM_MARKER}\nVolunteering via bernstein."
    return {
        "body": body,
        "createdAt": _iso(NOW - age),
        "url": f"https://github.com/{REPO}/issues/{NUMBER}#issuecomment-{comment_id}",
        "viewerDidAuthor": mine,
    }


def _issue_payload(
    *, closed: bool = False, assignees: list[Any] | None = None, comments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "number": NUMBER,
        "state": "CLOSED" if closed else "OPEN",
        "closed": closed,
        "assignees": assignees or [],
        "comments": comments or [],
    }


@dataclass
class FakeRunner:
    """Stub for the ``gh`` CLI; routes argument vectors to in-memory state.

    Modelled on ``tests/unit/orchestration/test_issue_to_pr.py`` but dispatches
    the ``gh issue view --json`` shape this module reads, which that fake does
    not.  ``next_id`` is handed out on POST so a test can assert an edit targets
    the id a post returned.
    """

    issue: dict[str, Any] = field(default_factory=_issue_payload)
    next_id: int = 555
    posts: list[dict[str, Any]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    fail_view: bool = False

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["issue", "view"]:
            if self.fail_view:
                return self._done(1, "")
            return self._done(0, json.dumps(self.issue))
        if args[:3] == ["api", "-X", "POST"]:
            comment_id = self.next_id
            self.next_id += 1
            self.posts.append({"url": args[3], "body": json.loads(stdin or "{}").get("body")})
            return self._done(0, json.dumps({"id": comment_id}))
        if args[:3] == ["api", "-X", "PATCH"]:
            self.patches.append({"url": args[3], "body": json.loads(stdin or "{}").get("body")})
            return self._done(0, "{}")
        raise AssertionError(f"unexpected gh call: {args}")

    @staticmethod
    def _done(code: int, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["gh"], returncode=code, stdout=stdout, stderr="boom" if code else "")


def _state(runner: FakeRunner):
    state = ClaimClient(runner=runner).fetch_state(REPO, NUMBER)
    assert state is not None
    return state


# ---------------------------------------------------------------------------
# should_skip: the five named cases (skip logic)
# ---------------------------------------------------------------------------


def test_assigned_issue_is_skipped() -> None:
    runner = FakeRunner(issue=_issue_payload(assignees=[{"login": "someone"}]))
    decision = should_skip(_state(runner), now=NOW, staleness=DEFAULT_CLAIM_STALENESS)
    assert decision.skip
    assert decision.reason is not None
    assert decision.reason.value == "assigned"


def test_fresh_other_claim_is_skipped() -> None:
    runner = FakeRunner(issue=_issue_payload(comments=[_comment(1, mine=False, age=timedelta(hours=1))]))
    decision = should_skip(_state(runner), now=NOW, staleness=DEFAULT_CLAIM_STALENESS)
    assert decision.skip
    assert decision.reason is not None
    assert decision.reason.value == "fresh_claim"


def test_stale_claim_is_treated_as_free() -> None:
    # The fixture hands the code a real "...Z" string, so the parse path is what
    # is under test -- not a datetime built in the test and trusted blindly.
    runner = FakeRunner(issue=_issue_payload(comments=[_comment(1, mine=False, age=timedelta(hours=7))]))
    state = _state(runner)
    assert state.claims[0].created_at == datetime(2026, 8, 18, 5, 0, 0, tzinfo=UTC)
    decision = should_skip(state, now=NOW, staleness=timedelta(hours=6))
    assert not decision.skip
    assert decision.reason is None


def test_resolved_other_claim_is_treated_as_free() -> None:
    # A released or completed claim keeps CLAIM_MARKER -- it must still be
    # found and still render as a claim -- but also carries RESOLVED_MARKER.
    # GitHub does not move a comment's createdAt on an edit, so without
    # checking `resolved` this comment would look exactly as fresh as an
    # active claim for the rest of the staleness window (acceptance case 5).
    runner = FakeRunner(
        issue=_issue_payload(comments=[_comment(1, mine=False, age=timedelta(hours=1), resolved=True)])
    )
    decision = should_skip(_state(runner), now=NOW, staleness=DEFAULT_CLAIM_STALENESS)
    assert not decision.skip
    assert decision.reason is None


def test_own_fresh_claim_is_not_skipped() -> None:
    # viewerDidAuthor is True: a restarted worker seeing its own fresh claim must
    # resume, not treat itself as a competitor and lock itself out.
    runner = FakeRunner(issue=_issue_payload(comments=[_comment(9, mine=True, age=timedelta(hours=1))]))
    state = _state(runner)
    decision = should_skip(state, now=NOW, staleness=DEFAULT_CLAIM_STALENESS)
    assert not decision.skip
    own = find_own_claim(state)
    assert own is not None
    assert own.rest_id == 9


# ---------------------------------------------------------------------------
# Client: post-once-then-edit, scoped by author
# ---------------------------------------------------------------------------


def test_claim_posted_once_then_updated_not_duplicated() -> None:
    # The acceptance criterion for case 4: a follow-up edit is a PATCH against
    # the id the POST returned, never a second POST.
    runner = FakeRunner()
    client = ClaimClient(runner=runner)

    comment_id = client.post_claim(REPO, NUMBER, build_claim_body(fingerprint="abc123", window=DEFAULT_CLAIM_STALENESS))
    assert comment_id == 555

    assert client.edit_claim(REPO, comment_id, "done")

    assert len(runner.posts) == 1
    assert len(runner.patches) == 1
    assert runner.patches[0]["url"].endswith(f"/issues/comments/{comment_id}")


def test_edit_targets_only_own_comment_id() -> None:
    # find_own_claim returns the caller's own comment even in a thread that also
    # carries another donor's claim; the id it yields is what an edit uses, so an
    # edit can never land on the other donor's comment (which would 403).
    runner = FakeRunner(
        issue=_issue_payload(
            comments=[
                _comment(100, mine=False, age=timedelta(hours=2)),
                _comment(200, mine=True, age=timedelta(hours=1)),
            ]
        )
    )
    own = find_own_claim(_state(runner))
    assert own is not None
    assert own.rest_id == 200


# ---------------------------------------------------------------------------
# repo_slug: only a URL with a host names a claimable GitHub issue
# ---------------------------------------------------------------------------


def test_repo_slug_rejects_urls_with_no_host() -> None:
    # A bare local path and an explicit file:// URL are both legitimate
    # repo_url values (a fixture repo, an already-mirrored clone -- see
    # repo_url_problem) and neither names a GitHub owner/repo to comment on.
    # An scp-style remote (git@host:owner/repo) gives urlparse no netloc
    # either, so it is correct-by-omission here rather than 404ing.
    assert repo_slug("/home/donor/src/torvalds/linux") is None
    assert repo_slug("file:///srv/mirrors/acme/widgets") is None
    assert repo_slug("git@github.com:owner/repo.git") is None


def test_repo_slug_accepts_urls_with_a_host() -> None:
    assert repo_slug("https://github.com/acme/widgets") == "acme/widgets"
    assert repo_slug("https://github.com/acme/widgets.git") == "acme/widgets"
    assert repo_slug("ssh://git@github.com/acme/widgets.git") == "acme/widgets"


# ---------------------------------------------------------------------------
# resolve_fingerprint: no silent fallback to a meaningless sentinel
# ---------------------------------------------------------------------------


def test_resolve_fingerprint_requires_an_explicit_value() -> None:
    # This used to fall back to the install-rev token, which returns the same
    # 16-zero sentinel on every donor's machine unless an operator opted into
    # identity emission -- so every claim comment carried an identical,
    # meaningless fingerprint.  Silence is the wrong failure mode for that.
    with pytest.raises(ValueError, match="claim_fingerprint"):
        resolve_fingerprint(None)
    with pytest.raises(ValueError, match="claim_fingerprint"):
        resolve_fingerprint("")


def test_resolve_fingerprint_returns_the_explicit_value() -> None:
    assert resolve_fingerprint("wk-explicit") == "wk-explicit"


# ---------------------------------------------------------------------------
# Defensive branches: a donor-fleet condition, never an exception
# ---------------------------------------------------------------------------


def test_a_naive_timestamp_drops_the_claim_rather_than_raising() -> None:
    # gh always emits a zone-qualified timestamp; this exercises the
    # defensive branch for the day it does not, rather than letting a naive
    # datetime reach `now - claim.created_at` in should_skip and raise
    # TypeError.
    comment = _comment(1, mine=False, age=timedelta(hours=1))
    comment["createdAt"] = "2026-08-18T11:00:00"  # no trailing Z / UTC offset
    runner = FakeRunner(issue=_issue_payload(comments=[comment]))
    state = _state(runner)
    assert state.claims == ()


def test_a_missing_gh_binary_does_not_raise_out_of_fetch_state(monkeypatch: Any) -> None:
    # A donor box without gh on PATH raises FileNotFoundError (an OSError
    # subclass) from subprocess.run itself, before the module's own
    # `returncode != 0` handling gets a look at it.  Claim etiquette must
    # never fail a run that would otherwise succeed.
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert ClaimClient().fetch_state(REPO, NUMBER) is None


# ---------------------------------------------------------------------------
# run_claimed_task wiring: short-circuit and release
# ---------------------------------------------------------------------------


def _task(repo_url: str) -> ClaimedTask:
    return ClaimedTask(repo_url=repo_url, issue_number=NUMBER, issue_title="t", issue_body="b")


def _donor() -> DonorLimits:
    return DonorLimits(available_backends=("container",), accepts_plain_container=True, wall_clock_minutes=1)


def _manifest() -> Any:
    from bernstein.core.volunteer.manifest import load_manifest

    return load_manifest(
        json.dumps(
            {
                "version": 1,
                "license": "Apache-2.0",
                "gates": [["true"]],
                "sandbox": "container",
                "max_wall_clock_minutes": 5,
            }
        )
    )


def test_claim_taken_short_circuits_before_clone(tmp_path: Path) -> None:
    # A fresh claim by another donor makes run_claimed_task refuse on
    # CLAIM_TAKEN before it clones, and without posting a claim of its own.
    runner = FakeRunner(issue=_issue_payload(comments=[_comment(1, mine=False, age=timedelta(hours=1))]))
    reached: list[AgentInvocation] = []

    def _never(invocation: AgentInvocation) -> Sequence[str]:
        reached.append(invocation)
        return []

    outcome = run_claimed_task(
        # A URL with a host, not a bare local path: repo_slug names no owner
        # for a host-less URL (test_repo_slug_rejects_urls_with_no_host), and
        # this test's whole point is that the CLAIM_TAKEN check does engage.
        _task(f"https://github.com/{REPO}"),
        _manifest(),
        donor=_donor(),
        workspace=tmp_path / "run",
        agent_argv=_never,
        sanitize=lambda text: text,
        claim=ClaimClient(runner=runner),
        now=lambda: NOW,
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.CLAIM_TAKEN
    assert outcome.reason == "fresh_claim"
    assert reached == []
    assert runner.posts == []


#: A host that resolves (loopback) with nothing listening on the port: git
#: fails with connection-refused immediately, no DNS and no real network
#: involved, so the clone step fails fast and deterministically in a "fast
#: unit test, no network" file while still giving repo_slug a real netloc.
_UNREACHABLE_REPO_URL = f"http://127.0.0.1:1/{REPO}"


def test_abort_releases_the_claim(tmp_path: Path) -> None:
    # No fresh claim: the worker posts one, the clone fails (connection
    # refused), and the abort edits the posted comment to a release -- one
    # post, one patch, and the patch targets the id the post returned.
    runner = FakeRunner()

    outcome = run_claimed_task(
        _task(_UNREACHABLE_REPO_URL),
        _manifest(),
        donor=_donor(),
        workspace=tmp_path / "run",
        agent_argv=_no_agent,
        sanitize=lambda text: text,
        claim=ClaimClient(runner=runner),
        claim_fingerprint="wk-abort-test",
        now=lambda: NOW,
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.CLONE
    assert len(runner.posts) == 1
    assert len(runner.patches) == 1
    assert runner.patches[0]["url"].endswith("/issues/comments/555")
    assert "Released" in runner.patches[0]["body"]


def test_stale_own_claim_is_not_reused(tmp_path: Path) -> None:
    # A worker's own claim from a previous run, more than a staleness window
    # old, must not be reused just because it is the worker's own: reusing it
    # skips posting a fresh claim (the task then looks unclaimed to every
    # other donor while this run holds it), and on abort the release edit
    # would land on that old comment and overwrite whatever it already
    # recorded (acceptance case 5, the reuse half of it).
    runner = FakeRunner(issue=_issue_payload(comments=[_comment(9, mine=True, age=timedelta(hours=20))]))

    outcome = run_claimed_task(
        _task(_UNREACHABLE_REPO_URL),
        _manifest(),
        donor=_donor(),
        workspace=tmp_path / "run",
        agent_argv=_no_agent,
        sanitize=lambda text: text,
        claim=ClaimClient(runner=runner),
        claim_fingerprint="wk-stale-test",
        now=lambda: NOW,
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.CLONE
    # A fresh claim was posted (id 555) rather than reusing the 20h-old own
    # claim (id 9), and the release on abort edits that new comment.
    assert len(runner.posts) == 1
    assert len(runner.patches) == 1
    assert runner.patches[0]["url"].endswith("/issues/comments/555")


def test_resolved_own_claim_is_not_reused_even_if_fresh(tmp_path: Path) -> None:
    # A worker's own claim that is already a completion or a release must not
    # be reused either, even minutes old: it is a record of a *previous*,
    # already-finished run, and editing it again would destroy that record.
    runner = FakeRunner(
        issue=_issue_payload(comments=[_comment(9, mine=True, age=timedelta(minutes=5), resolved=True)])
    )

    outcome = run_claimed_task(
        _task(_UNREACHABLE_REPO_URL),
        _manifest(),
        donor=_donor(),
        workspace=tmp_path / "run",
        agent_argv=_no_agent,
        sanitize=lambda text: text,
        claim=ClaimClient(runner=runner),
        claim_fingerprint="wk-resolved-test",
        now=lambda: NOW,
    )

    assert isinstance(outcome, TaskRefusal)
    assert len(runner.posts) == 1
    assert runner.patches[0]["url"].endswith("/issues/comments/555")
