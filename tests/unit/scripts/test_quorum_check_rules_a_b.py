import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

# Import modules from scripts
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../scripts")))
import quorum_check

UTC = UTC
now = datetime.now(UTC)


def _pr(author="outsider", changed_lines=100, head_sha="head", paths=None, reviews=None, review_requests=None):
    return quorum_check.PullRequest(
        number=1,
        author=author,
        is_draft=False,
        head_sha=head_sha,
        changed_lines=changed_lines,
        paths=paths or ["src/bernstein/app.py"],
        reviews=reviews or [],
        contributors=set(),
        last_push=now,
        review_requests=review_requests or {},
    )


def _review(login, state, commit_id="head", hours_ago=0):
    t = (now - timedelta(hours=hours_ago)).isoformat()
    return quorum_check.Review(login, state, commit_id, t)


@pytest.fixture
def roster():
    return quorum_check.Roster(
        maintainer="owner",
        core_reviewers=frozenset({"core1", "core2"}),
        committers=frozenset({"comm1", "comm2"}),
        automation=frozenset({"bot"}),
        machine_reviewers=frozenset({"machine"}),
    )


def test_rule_a_stale_objection(roster):
    # lapses at 72 h
    req_time = (now - timedelta(hours=72)).isoformat()
    pr = _pr(
        reviews=[_review("core1", "CHANGES_REQUESTED", commit_id="old", hours_ago=73)],
        review_requests={"core1": req_time},
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert not any("changes requested" in req.text for req in v.requirements if not req.met)

    # holds at 71 h
    req_time = (now - timedelta(hours=71)).isoformat()
    pr = _pr(
        reviews=[_review("core1", "CHANGES_REQUESTED", commit_id="old", hours_ago=73)],
        review_requests={"core1": req_time},
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert any("changes requested" in req.text for req in v.requirements if not req.met)

    # holds forever on current head
    pr = _pr(
        reviews=[_review("core1", "CHANGES_REQUESTED", commit_id="head", hours_ago=100)],
        review_requests={"core1": req_time},
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert any("changes requested" in req.text for req in v.requirements if not req.met)

    # holds without a re-request
    pr = _pr(reviews=[_review("core1", "CHANGES_REQUESTED", commit_id="old", hours_ago=73)])
    v = quorum_check.evaluate(pr, roster, [], now)
    assert any("changes requested" in req.text for req in v.requirements if not req.met)

    # new CR on new head holds again
    pr = _pr(
        reviews=[
            _review("core1", "CHANGES_REQUESTED", commit_id="old", hours_ago=73),
            _review("core1", "CHANGES_REQUESTED", commit_id="head", hours_ago=1),
        ],
        review_requests={"core1": req_time},
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert any("changes requested" in req.text for req in v.requirements if not req.met)


def test_rule_a1_sensitive_large(roster):
    # sensitive 900 lines + maintainer at 168h -> met
    pr = _pr(paths=["sandbox/foo.py"], changed_lines=900, reviews=[_review("owner", "APPROVED", hours_ago=168)])
    v = quorum_check.evaluate(pr, roster, [], now)
    assert v.passed

    # at 100h -> not met, who reads 'about 68h'
    pr = _pr(paths=["sandbox/foo.py"], changed_lines=900, reviews=[_review("owner", "APPROVED", hours_ago=100)])
    v = quorum_check.evaluate(pr, roster, [], now)
    assert not v.passed
    assert any("about 68h" in req.who for req in v.requirements if not req.met)

    # 1,500 ordinary lines + maintainer at 168h -> met
    pr = _pr(paths=["src/bernstein/app.py"], changed_lines=1500, reviews=[_review("owner", "APPROVED", hours_ago=168)])
    v = quorum_check.evaluate(pr, roster, [], now)
    assert v.passed

    # at 72h -> not met
    pr = _pr(paths=["src/bernstein/app.py"], changed_lines=1500, reviews=[_review("owner", "APPROVED", hours_ago=72)])
    v = quorum_check.evaluate(pr, roster, [], now)
    assert not v.passed

    # `core/identity/` 500 lines + maintainer at 168h -> not met
    pr = _pr(
        paths=["src/bernstein/core/identity/foo.py"],
        changed_lines=500,
        reviews=[_review("owner", "APPROVED", hours_ago=168)],
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert not v.passed

    # `core/identity/` 300 lines at 168h -> met
    pr = _pr(
        paths=["src/bernstein/core/identity/foo.py"],
        changed_lines=300,
        reviews=[_review("owner", "APPROVED", hours_ago=168)],
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert v.passed

    # a committer's changes requested inside the window -> not met
    pr = _pr(
        paths=["sandbox/foo.py"],
        changed_lines=900,
        reviews=[_review("owner", "APPROVED", hours_ago=168), _review("comm1", "CHANGES_REQUESTED", hours_ago=1)],
    )
    v = quorum_check.evaluate(pr, roster, [], now)
    assert not v.passed


def test_rule_a2_unanswered_objection_dismissed_does_not_block(roster):
    # a DISMISSED review from a core reviewer does not block
    pr = _pr(reviews=[_review("core1", "APPROVED", hours_ago=10), _review("core2", "DISMISSED", hours_ago=10)])
    v = quorum_check.evaluate(pr, roster, [], now)
    # The requirement about "changes requested" should be met (since it's dismissed)
    assert not any("changes requested" in req.text for req in v.requirements if not req.met)
