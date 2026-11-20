"""`scripts/quorum_request_reviews.py`: who gets asked for the reviews a change still needs."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "quorum_request_reviews.py"
HEAD = "d" * 40
OLD = "e" * 40


@pytest.fixture(scope="module")
def rr() -> ModuleType:
    spec = importlib.util.spec_from_file_location("quorum_request_reviews_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def roster(rr: ModuleType):
    return rr.qc.Roster(
        maintainer="owner",
        core_reviewers=frozenset({"core1", "core2"}),
        committers=frozenset({"comm1", "comm2"}),
        automation=frozenset({"the-conductor[bot]"}),
    )


def _review(rr: ModuleType, login: str, state: str, *, sha: str = HEAD):
    return rr.qc.Review(login=login, state=state, commit_id=sha, submitted_at="2026-09-10T10:00:00Z")


def _pr(
    rr: ModuleType, *, author: str = "outsider", reviews=None, paths=None, changed_lines: int = 100, contributors=None
):
    return rr.qc.PullRequest(
        number=7,
        author=author,
        is_draft=False,
        head_sha=HEAD,
        changed_lines=changed_lines,
        paths=paths or ["src/bernstein/adapters/aider.py"],
        reviews=reviews or [],
        contributors=contributors if contributors is not None else {author},
        last_push=datetime(2026, 9, 10, 11, 0, tzinfo=UTC) - timedelta(hours=1),
        commit_authors=frozenset({author}),
        commit_authors_complete=True,
    )


def test_asks_two_with_a_core_reviewer_first_and_lowest_load(rr, roster) -> None:
    load = {"core1": 2, "core2": 0, "comm1": 0, "comm2": 1}
    assert rr.plan_requests(_pr(rr), roster, set(), load) == ["core2", "comm1"]


def test_a_core_approval_leaves_one_more_to_ask_from_anyone(rr, roster) -> None:
    pr = _pr(rr, reviews=[_review(rr, "core1", "APPROVED")])
    assert rr.plan_requests(pr, roster, set(), {}) == ["comm1"]


def test_a_committer_approval_still_needs_a_core_reviewer(rr, roster) -> None:
    pr = _pr(rr, reviews=[_review(rr, "comm1", "APPROVED")])
    assert rr.plan_requests(pr, roster, set(), {"core1": 1}) == ["core2"]


def test_a_standing_request_counts_and_is_not_repeated(rr, roster) -> None:
    assert rr.plan_requests(_pr(rr), roster, {"core1"}, {}) == ["comm1"]
    assert rr.plan_requests(_pr(rr), roster, {"core1", "comm2"}, {}) == []


def test_author_pushers_coauthors_and_the_maintainer_are_never_asked(rr, roster) -> None:
    pr = _pr(rr, contributors={"outsider", "core1", "comm1"})
    assert rr.plan_requests(pr, roster, set(), {}) == ["core2", "comm2"]


def test_maintainer_and_automation_changes_ask_nobody(rr, roster) -> None:
    assert rr.plan_requests(_pr(rr, author="owner", contributors={"owner"}), roster, set(), {}) == []
    assert rr.plan_requests(_pr(rr, author="the-conductor[bot]"), roster, set(), {}) == []


def test_maintainer_carried_commit_requests_sensitive_quorum(rr, roster) -> None:
    pr = _pr(rr, author="owner", contributors={"owner", "outsider"}, paths=["src/bernstein/core/security/policy.py"])
    pr.commit_authors = frozenset({"owner", "outsider"})
    pr.commit_authors_complete = True
    assert rr.plan_requests(pr, roster, set(), {}) == ["core1", "core2", "comm1"]


def test_maintainer_incomplete_history_requests_nobody(rr, roster) -> None:
    pr = _pr(rr, author="owner", contributors={"owner", "outsider"})
    pr.commit_authors = frozenset({"owner", "outsider"})
    pr.commit_authors_complete = False
    assert rr.plan_requests(pr, roster, set(), {}) == []


def test_a_standing_changes_requested_pauses_the_asking(rr, roster) -> None:
    pr = _pr(rr, reviews=[_review(rr, "comm2", "CHANGES_REQUESTED")])
    assert rr.plan_requests(pr, roster, set(), {}) == []


def test_a_stale_approval_means_that_reviewer_is_asked_again(rr, roster) -> None:
    pr = _pr(rr, reviews=[_review(rr, "core1", "APPROVED", sha=OLD)])
    assert rr.plan_requests(pr, roster, set(), {"core2": 1, "comm1": 1, "comm2": 1}) == ["core1", "comm1"]


def test_a_sensitive_change_asks_for_three_with_two_core(rr, roster) -> None:
    pr = _pr(rr, paths=["src/bernstein/core/security/policy.py"])
    assert rr.plan_requests(pr, roster, set(), {}) == ["core1", "core2", "comm1"]


def test_the_per_reviewer_cap_skips_the_overloaded(rr, roster) -> None:
    load = {"core1": 3, "core2": 3, "comm1": 0, "comm2": 0}
    assert rr.plan_requests(_pr(rr), roster, set(), load) == ["comm1", "comm2"]


def test_request_load_counts_standing_requests(rr) -> None:
    prs = [{"requested_reviewers": [{"login": "a"}, {"login": "b"}]}, {"requested_reviewers": [{"login": "a"}]}]
    assert rr.request_load(prs) == {"a": 2, "b": 1}


def test_human_changes_get_the_full_evaluation_before_author_classification(rr, roster) -> None:
    assert rr.needs_a_look({"user": {"login": "outsider"}, "draft": False}, roster)
    assert not rr.needs_a_look({"user": {"login": "outsider"}, "draft": True}, roster)
    assert rr.needs_a_look({"user": {"login": "owner"}, "draft": False}, roster)
    assert not rr.needs_a_look({"user": {"login": "the-conductor[bot]"}, "draft": False}, roster)
    assert not rr.needs_a_look({"user": {"login": "github-actions[bot]"}, "draft": False}, roster)


@pytest.mark.parametrize(
    "path,owners,requested,expected",
    [
        ("docs/guide.md", [], [], "@comm1"),
        ("docs/guide.md", [], ["comm2"], None),
        ("docs/guide.md", [("/docs/", ["core1"])], [], "@core1, @core2, @comm1"),
        ("docs/script.js", [], [], "@core1, @core2, @comm1"),
    ],
)
def test_hourly_sweep_invites_only_the_needed_eligible_reviewers(
    rr, roster, monkeypatch, capsys, path, owners, requested, expected
):
    pr = _pr(rr, paths=[path], changed_lines=401)
    raw = {"number": pr.number, "user": {"login": pr.author}, "requested_reviewers": [{"login": n} for n in requested]}
    monkeypatch.setattr(rr, "open_pull_requests", lambda repo: [raw])
    monkeypatch.setattr(rr.qc, "fetch_pull_request", lambda repo, number: pr)
    monkeypatch.setattr(rr.qc, "load_roster", lambda root: roster)
    monkeypatch.setattr(rr.qc, "load_codeowners", lambda root: owners)
    assert rr.main(["--repo", "example/project", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == ([f"#7: would request {expected}"] if expected else [])


def test_requester_counts_current_eligible_people_once_and_fills_missing_core(rr, roster):
    pr = _pr(rr, paths=["web/tool.ts"], changed_lines=401, contributors={"outsider", "core1"})
    pr.reviews = [_review(rr, "comm1", "APPROVED", sha=OLD), _review(rr, "comm2", "APPROVED")]
    pr.same_diff = frozenset({OLD})
    pr.update_only = frozenset({"core1"})
    assert rr.plan_requests(pr, roster, {"comm1", "core2"}, {}) == ["core1"]


@pytest.mark.parametrize("lines,expected", [(10, ["core1", "comm1"]), (401, ["core1", "core2", "comm1"])])
def test_incomplete_file_evidence_requests_the_ordinary_quorum(rr, roster, lines, expected):
    pr = _pr(rr, paths=["docs/guide.md"], changed_lines=lines)
    pr.paths_complete = False
    pr.labels = frozenset({"size/s"})
    assert rr.plan_requests(pr, roster, set(), {}) == expected
