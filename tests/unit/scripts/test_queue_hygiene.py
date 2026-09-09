"""Unit tests for ``scripts/queue_hygiene.py``.

Each of the five rules is exercised directly against constructed
``PullRequest`` fixtures rather than through ``main()`` end to end, so a test
failure points at the one rule that regressed instead of at "something in the
gh subprocess chain broke". ``last_changes_requested_without_push`` is the one
function that talks to the network (via ``gh_json``); its two tests monkeypatch
that call rather than mocking the ``gh`` binary itself.

The full-PR-set-before-filtering test below pins a real bug found while this
script was first exercised against the live repo: ``--pr`` narrowed the input
list before the cross-PR rules ran, silently dropping both over-wip and
duplicate detection for the one PR left in. See the fix's own commit message
for how that was found.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "queue_hygiene.py"


@pytest.fixture(scope="module")
def qh() -> ModuleType:
    """Load ``scripts/queue_hygiene.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("queue_hygiene_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _pr(
    qh: ModuleType,
    number: int,
    author: str = "someone",
    *,
    age_days: int = 0,
    is_draft: bool = False,
    labels: set[str] | None = None,
    review_decision: str = "",
    body: str = "",
    checks_pass: bool = True,
    mergeable: str = "MERGEABLE",
    changed_lines: int = 100,
) -> object:
    """Build a ``PullRequest`` with sane defaults, oldest-first by age_days."""
    now = datetime.now(UTC)
    return qh.PullRequest(
        number=number,
        title=f"pr {number}",
        author=author,
        created_at=now - timedelta(days=age_days),
        is_draft=is_draft,
        labels=set(labels or set()),
        review_decision=review_decision,
        body=body,
        checks_pass=checks_pass,
        mergeable=mergeable,
        changed_lines=changed_lines,
    )


# --- over-wip -----------------------------------------------------------


def test_over_wip_leaves_the_oldest_five_clean(qh: ModuleType) -> None:
    # 7 PRs by the same author, oldest (highest age_days) first in creation
    # order. The oldest 5 must never get the label; the newest 2 must.
    prs = [_pr(qh, n, author="alice", age_days=10 - n) for n in range(1, 8)]
    qh.rule_over_wip(prs)
    by_number = {p.number: p for p in prs}
    for n in range(1, 6):  # 5 oldest (ages 9..5)
        assert not any(i.startswith("add:over-wip") for i in by_number[n].intents), n
    for n in range(6, 8):  # 2 newest
        assert any(i.startswith("add:over-wip") for i in by_number[n].intents), n


def test_over_wip_does_not_fire_under_the_cap(qh: ModuleType) -> None:
    prs = [_pr(qh, n, author="bob", age_days=n) for n in range(1, 5)]  # 4 open
    qh.rule_over_wip(prs)
    assert all(not p.intents for p in prs)


def test_over_wip_removes_a_stale_label_once_back_under_cap(qh: ModuleType) -> None:
    # Only 3 open PRs by this author now, but one still carries the label
    # from when they had more - the rule must offer to remove it.
    prs = [_pr(qh, n, author="carol", age_days=n, labels={"over-wip"} if n == 1 else set()) for n in range(1, 4)]
    qh.rule_over_wip(prs)
    by_number = {p.number: p for p in prs}
    assert any(i.startswith("remove:over-wip") for i in by_number[1].intents)


def test_over_wip_ignores_drafts_on_both_sides(qh: ModuleType) -> None:
    # A draft does not count toward the cap, and is never itself labelled.
    prs = [_pr(qh, n, author="dee", age_days=10 - n) for n in range(1, 6)]
    prs.append(_pr(qh, 6, author="dee", age_days=0, is_draft=True))
    qh.rule_over_wip(prs)
    assert all(not p.intents for p in prs)  # exactly 5 non-draft, at the cap


# --- duplicate ------------------------------------------------------------


def test_duplicate_labels_only_the_later_pr(qh: ModuleType) -> None:
    first = _pr(qh, 100, age_days=5, body="fixes #42")
    second = _pr(qh, 101, age_days=1, body="Closes #42")  # newer, same issue
    qh.rule_duplicate([first, second])
    assert not first.intents
    assert any(i.startswith("add:duplicate") for i in second.intents)
    assert "#100" in second.intents[0]


def test_duplicate_does_not_fire_on_a_single_referring_pr(qh: ModuleType) -> None:
    only = _pr(qh, 200, body="resolves #7")
    qh.rule_duplicate([only])
    assert not only.intents


def test_duplicate_matches_githubs_closing_keyword_set(qh: ModuleType) -> None:
    # close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved, case
    # insensitive, with or without a colon - the same set GitHub itself
    # recognizes for issue auto-closing.
    bodies = ["Fixes #9", "closed: #9", "This RESOLVES #9"]
    prs = [_pr(qh, 300 + i, age_days=3 - i, body=b) for i, b in enumerate(bodies)]
    qh.rule_duplicate(prs)
    assert not prs[0].intents  # oldest (highest age_days) is first, never labelled
    assert all(any(i.startswith("add:duplicate") for i in p.intents) for p in prs[1:])


def test_duplicate_dedupes_a_pr_referencing_the_same_issue_twice(qh: ModuleType) -> None:
    # A PullRequest is unhashable (mutable set/list fields) - rule_duplicate
    # must dedupe by .number, not by putting PullRequest objects in a set.
    # This is a regression test for exactly that TypeError.
    first = _pr(qh, 400, age_days=2, body="fixes #1")
    twice = _pr(qh, 401, age_days=1, body="fixes #1 and also fixes #1")
    qh.rule_duplicate([first, twice])  # must not raise
    assert any(i.startswith("add:duplicate") for i in twice.intents)
    assert len(twice.intents) == 1  # not double-added for the double mention


# --- needs-committer-review ------------------------------------------------


def test_needs_review_added_when_green_and_unblocked(qh: ModuleType) -> None:
    pr = _pr(qh, 1, checks_pass=True, review_decision="")
    qh.rule_needs_committer_review([pr])
    assert any(i.startswith("add:needs-committer-review") for i in pr.intents)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_draft": True},
        {"checks_pass": False},
        {"review_decision": "CHANGES_REQUESTED"},
    ],
)
def test_needs_review_not_added_when_blocked(qh: ModuleType, kwargs: dict) -> None:
    # _pr() already defaults checks_pass=True; kwargs overrides exactly the
    # one condition each parametrized case is testing.
    pr = _pr(qh, 1, **kwargs)
    qh.rule_needs_committer_review([pr])
    assert not any(i.startswith("add:needs-committer-review") for i in pr.intents)


def test_needs_review_not_added_on_a_merge_conflict(qh: ModuleType) -> None:
    pr = _pr(qh, 1, mergeable="CONFLICTING")
    qh.rule_needs_committer_review([pr])
    assert not any(i.startswith("add:needs-committer-review") for i in pr.intents)


def test_needs_review_not_added_while_mergeable_is_unknown(qh: ModuleType) -> None:
    # mergeable is computed asynchronously by GitHub and reads UNKNOWN until
    # it catches up (and every merge to the base branch invalidates it again) -
    # that is not the same as a confirmed absence of a conflict.
    pr = _pr(qh, 1, mergeable="UNKNOWN")
    qh.rule_needs_committer_review([pr])
    assert not any(i.startswith("add:needs-committer-review") for i in pr.intents)


def test_needs_review_removed_once_a_conflict_appears(qh: ModuleType) -> None:
    pr = _pr(qh, 1, mergeable="CONFLICTING", labels={"needs-committer-review"})
    qh.rule_needs_committer_review([pr])
    assert any(i.startswith("remove:needs-committer-review") for i in pr.intents)


def test_needs_review_removed_once_no_longer_eligible(qh: ModuleType) -> None:
    pr = _pr(qh, 1, checks_pass=False, labels={"needs-committer-review"})
    qh.rule_needs_committer_review([pr])
    assert any(i.startswith("remove:needs-committer-review") for i in pr.intents)


# --- changes-requested timeout ---------------------------------------------


def test_changes_requested_timeout_fires_after_14_days_no_push(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat().replace("+00:00", "Z")
    calls = {"reviews": [{"state": "CHANGES_REQUESTED", "submitted_at": old}], "commits": []}

    def fake_gh_json(*args: str) -> object:
        if "reviews" in args[1]:
            return calls["reviews"]
        return calls["commits"]

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, review_decision="CHANGES_REQUESTED")
    qh.rule_changes_requested_timeout("owner/repo", [pr])
    assert any(i.startswith("close (") for i in pr.intents)


def test_changes_requested_timer_resets_on_a_later_push(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat().replace("+00:00", "Z")
    recent_push = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    calls = {
        "reviews": [{"state": "CHANGES_REQUESTED", "submitted_at": old}],
        "commits": [{"commit": {"committer": {"date": recent_push}}}],
    }

    def fake_gh_json(*args: str) -> object:
        if "reviews" in args[1]:
            return calls["reviews"]
        return calls["commits"]

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, review_decision="CHANGES_REQUESTED")
    qh.rule_changes_requested_timeout("owner/repo", [pr])
    assert not pr.intents  # a push since the request resets the 14-day timer


def test_changes_requested_timeout_respects_exempt_labels(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh_json(*args: str) -> object:
        raise AssertionError("must not call the API for an exempt PR")

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, review_decision="CHANGES_REQUESTED", labels={"work-in-progress"})
    qh.rule_changes_requested_timeout("owner/repo", [pr])
    assert not pr.intents


# --- approval-shape ---------------------------------------------------------


def _review(review_id: int, login: str, state: str, at: str, *, body: str = "", user_type: str | None = None) -> dict:
    user = {"login": login}
    if user_type is not None:
        user["type"] = user_type
    return {"id": review_id, "user": user, "state": state, "submitted_at": at, "body": body}


def _install_review_api(monkeypatch: pytest.MonkeyPatch, qh: ModuleType, reviews: list, comments: list) -> None:
    def fake_gh_json(*args: str) -> object:
        if args[1].endswith("/reviews"):
            return reviews
        if args[1].endswith("/comments"):
            return comments
        raise AssertionError(f"unexpected call {args}")

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)


def test_approval_shape_dismisses_a_bare_approval_on_a_non_trivial_change(
    qh: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_review_api(monkeypatch, qh, [_review(10, "alice", "APPROVED", "2026-09-10T10:00:00Z")], [])
    pr = _pr(qh, 1, changed_lines=120)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert pr.intents == ["dismiss:10 (alice: approval without a line comment on 120 changed lines)"]


def test_approval_shape_keeps_an_approval_whose_author_left_a_line_comment(
    qh: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The line comment sits on a different review id (added after approving):
    # what matters is that the approver read a line, not which review carried it.
    _install_review_api(
        monkeypatch,
        qh,
        [_review(10, "alice", "APPROVED", "2026-09-10T10:00:00Z")],
        [{"user": {"login": "alice"}}],
    )
    pr = _pr(qh, 1, changed_lines=120)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_approval_shape_keeps_an_approval_whose_body_is_non_blank(
    qh: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No line comment anywhere, but the review's own body is a real note - the
    # charter's criterion is "any comment", not specifically a line-level one.
    _install_review_api(
        monkeypatch,
        qh,
        [
            _review(
                10,
                "alice",
                "APPROVED",
                "2026-09-10T10:00:00Z",
                body="Ran the suite locally, checked the migration path, LGTM",
            )
        ],
        [],
    )
    pr = _pr(qh, 1, changed_lines=120)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_approval_shape_keeps_an_approval_from_before_the_effective_date(
    qh: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First arming must not re-litigate every approval already standing on the
    # open queue - only approvals given at or after APPROVAL_SHAPE_EFFECTIVE_FROM
    # are ever dismissed.
    before_cutoff = "2026-09-09T23:59:59Z"
    assert before_cutoff < qh.APPROVAL_SHAPE_EFFECTIVE_FROM
    _install_review_api(monkeypatch, qh, [_review(10, "alice", "APPROVED", before_cutoff)], [])
    pr = _pr(qh, 1, changed_lines=120)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_approval_shape_respects_exempt_labels(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same convention the changes-requested timeout already honors - pinned,
    # do-not-close and work-in-progress must protect a PR from an automated
    # dismissal too, not only from an automated close.
    def fake_gh_json(*args: str) -> object:
        raise AssertionError("must not call the API for an exempt PR")

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, changed_lines=200, labels={"pinned"})
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_approval_shape_does_not_redismiss_a_bare_re_approval(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # carol's original bare approval was dismissed by an earlier run - dismissing
    # a review sets its own body to the dismissal message, so that history is
    # still visible as a DISMISSED review carrying it. She re-approved bare
    # again without adding a comment; the once-only guard must leave the new
    # review alone rather than dismiss it too.
    reviews = [
        _review(10, "carol", "DISMISSED", "2026-09-10T09:00:00Z", body=qh.DISMISSAL_MESSAGE),
        _review(11, "carol", "APPROVED", "2026-09-11T09:00:00Z"),
    ]
    _install_review_api(monkeypatch, qh, reviews, [])
    pr = _pr(qh, 1, changed_lines=200)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_approval_shape_ignores_small_changes_without_calling_the_api(
    qh: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_gh_json(*args: str) -> object:
        raise AssertionError("must not call the API for a small change")

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, changed_lines=qh.APPROVAL_SHAPE_MIN_CHANGED_LINES)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_approval_shape_exempts_the_maintainer_and_bots(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # "chernistry" is the script's own --maintainer default (main()'s argparse);
    # the rule itself takes whatever login is threaded in, so this test states
    # the value explicitly rather than reading it back off the module.
    maintainer = "chernistry"
    _install_review_api(
        monkeypatch,
        qh,
        [
            _review(10, maintainer, "APPROVED", "2026-09-10T10:00:00Z"),
            # Suffix-recognized bot (renovate[bot]) ...
            _review(11, "renovate[bot]", "APPROVED", "2026-09-10T10:00:00Z"),
            # ... and a REST app user with no [bot] suffix at all - recognized
            # only because the endpoint carries user.type == "Bot". Dated on
            # or after the cutoff so this exercises the bot check itself,
            # not the effective-date filter.
            _review(12, "some-ci-bot", "APPROVED", "2026-09-10T10:00:00Z", user_type="Bot"),
        ],
        [],
    )
    pr = _pr(qh, 1, changed_lines=500)
    qh.rule_approval_shape("owner/repo", [pr], maintainer=maintainer)
    assert not pr.intents


def test_approval_shape_uses_each_users_latest_verdict(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    reviews = [
        # alice approved bare, then requested changes: nothing standing to dismiss
        _review(10, "alice", "APPROVED", "2026-09-11T10:00:00Z"),
        _review(11, "alice", "CHANGES_REQUESTED", "2026-09-11T11:00:00Z"),
        # bob approved bare, then re-approved and left a line note: the fresh one stands
        _review(20, "bob", "APPROVED", "2026-09-11T10:00:00Z"),
        _review(21, "bob", "APPROVED", "2026-09-11T12:00:00Z"),
        # carol approved bare, then only commented: GitHub keeps her approval standing
        _review(30, "carol", "APPROVED", "2026-09-11T10:00:00Z"),
        _review(31, "carol", "COMMENTED", "2026-09-11T13:00:00Z"),
    ]
    comments = [{"user": {"login": "bob"}}]
    _install_review_api(monkeypatch, qh, reviews, comments)
    pr = _pr(qh, 1, changed_lines=200)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert pr.intents == ["dismiss:30 (carol: approval without a line comment on 200 changed lines)"]


def test_approval_shape_skips_drafts(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh_json(*args: str) -> object:
        raise AssertionError("must not call the API for a draft")

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, is_draft=True, changed_lines=999)
    qh.rule_approval_shape("owner/repo", [pr], maintainer="chernistry")
    assert not pr.intents


def test_apply_dismisses_through_the_review_dismissal_endpoint(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(qh, "gh", lambda *args: calls.append(args))
    pr = _pr(qh, 7)
    pr.intents.append("dismiss:10 (alice: approval without a line comment on 120 changed lines)")
    qh.apply_intents("owner/repo", pr)
    assert len(calls) == 1
    assert calls[0][:4] == ("api", "-X", "PUT", "repos/owner/repo/pulls/7/reviews/10/dismissals")
    assert calls[0][-1].startswith("message=Dismissed by queue hygiene")


def test_a_failing_dismissal_does_not_stop_later_intents(qh: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # A dismissal can individually fail (branch protection restricting who may
    # dismiss, or a review someone already dismissed by hand) - one bad review
    # must not block the other intents queued after it in the same run.
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> None:
        calls.append(args)
        if args[0] == "api":
            raise subprocess.CalledProcessError(1, args, output="", stderr="422 already dismissed")

    monkeypatch.setattr(qh, "gh", fake_gh)
    pr = _pr(qh, 7)
    pr.intents.append("dismiss:10 (alice: approval without a line comment on 120 changed lines)")
    pr.intents.append(f"add:{qh.NEEDS_REVIEW_LABEL}")
    qh.apply_intents("owner/repo", pr)  # must not raise
    assert len(calls) == 2
    assert calls[0][:3] == ("api", "-X", "PUT")
    assert calls[1] == ("pr", "edit", "7", "--repo", "owner/repo", "--add-label", qh.NEEDS_REVIEW_LABEL)


# --- reviews are fetched once per PR, shared across rules ------------------


def test_reviews_are_fetched_once_per_pr_when_the_cache_is_shared(
    qh: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A PR that is both over the approval-shape line threshold and currently
    # CHANGES_REQUESTED qualifies for both rules, each of which used to fetch
    # /reviews on its own - count the stub calls to prove a shared cache
    # collapses that to one call.
    recent = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    endpoints_called: list[str] = []

    def fake_gh_json(*args: str) -> object:
        endpoints_called.append(args[1])
        if args[1].endswith("/reviews"):
            return [_review(10, "alice", "CHANGES_REQUESTED", recent)]
        if args[1].endswith("/comments"):
            return []
        if args[1].endswith("/commits"):
            return []
        raise AssertionError(f"unexpected call {args}")

    monkeypatch.setattr(qh, "gh_json", fake_gh_json)
    pr = _pr(qh, 1, changed_lines=200, review_decision="CHANGES_REQUESTED")
    reviews_cache: dict[int, list[dict]] = {}
    qh.rule_approval_shape("owner/repo", [pr], reviews_cache, maintainer="chernistry")
    qh.rule_changes_requested_timeout("owner/repo", [pr], reviews_cache)

    review_calls = [e for e in endpoints_called if e.endswith("/reviews")]
    assert len(review_calls) == 1


# --- --pr must narrow the OUTPUT, never the rules' INPUT --------------------


def test_pr_filter_does_not_weaken_cross_pr_rules(qh: ModuleType) -> None:
    """Regression test for the bug fixed alongside this file: filtering to
    one PR before the cross-PR rules ran silently dropped over-wip and
    duplicate detection for it, because each rule needs the full open-PR set
    to see an author's total count or a second PR against the same issue.
    The contract main() must uphold: run every rule over the full set first,
    filter what gets printed/acted on second.
    """
    # 6 PRs by the same author (1 over the cap of 5) - the target is the
    # newest, so it should be flagged over-wip only if the rules saw all 6.
    prs = [_pr(qh, n, author="eve", age_days=10 - n) for n in range(1, 7)]
    target = next(p for p in prs if p.number == 6)  # newest = 6th open

    qh.rule_over_wip(prs)
    qh.rule_duplicate(prs)
    qh.rule_needs_committer_review(prs)

    # Simulate main()'s post-rule --pr narrowing.
    filtered = [p for p in prs if p.number == 6]

    assert filtered == [target]
    assert any(i.startswith("add:over-wip") for i in target.intents), (
        "over-wip must still fire for the filtered PR - the rule needed to "
        "see all 6 of this author's PRs, not just the filtered-to-one list"
    )
