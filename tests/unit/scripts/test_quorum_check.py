"""Unit tests for ``scripts/quorum_check.py``.

Every rule is exercised against a constructed ``PullRequest`` through
``evaluate()``, which is the whole decision: the network calls sit in
``fetch_pull_request`` and are not part of what is being tested here, so a
failure names the rule that regressed rather than a broken subprocess chain.

The cases that matter most are the ones where a pass would be wrong: an
approval given before the last push, an approval from someone who pushed to
the branch, automation touching a path where it is not allowed to merge
itself, and the objection window on governance files.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "quorum_check.py"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
HEAD = "d" * 40


@pytest.fixture(scope="module")
def qc() -> ModuleType:
    """Load ``scripts/quorum_check.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("quorum_check_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def roster(qc: ModuleType):
    return qc.Roster(
        maintainer="owner",
        core_reviewers=frozenset({"core1", "core2"}),
        committers=frozenset({"comm1", "comm2"}),
        automation=frozenset({"the-conductor[bot]", "renovate[bot]"}),
    )


OWNERS = [("*", ["core1", "core2"]), ("/.github/", ["owner"]), ("/src/bernstein/core/", ["owner"])]


def _review(qc: ModuleType, login: str, state: str, *, sha: str = HEAD, at: str = "2026-09-10T10:00:00Z"):
    return qc.Review(login=login, state=state, commit_id=sha, submitted_at=at)


def _pr(
    qc: ModuleType,
    *,
    author: str = "outsider",
    is_draft: bool = False,
    changed_lines: int = 100,
    paths: list[str] | None = None,
    reviews: list | None = None,
    contributors: set[str] | None = None,
    pushed_hours_ago: float = 1.0,
):
    return qc.PullRequest(
        number=1,
        author=author,
        is_draft=is_draft,
        head_sha=HEAD,
        changed_lines=changed_lines,
        paths=paths if paths is not None else ["src/bernstein/adapters/aider.py"],
        reviews=reviews or [],
        contributors=contributors if contributors is not None else {author},
        last_push=NOW - timedelta(hours=pushed_hours_ago),
    )


def _evaluate(qc: ModuleType, roster, pr):
    return qc.evaluate(pr, roster, OWNERS, NOW)


# --- the ordinary contributor path -----------------------------------------


def test_two_approvals_with_one_core_pass(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")])
    assert _evaluate(qc, roster, pr).passed


def test_two_committer_approvals_without_a_core_one_fail(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "comm1", "APPROVED"), _review(qc, "comm2", "APPROVED")])
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "core reviewer" in verdict.requirements[0].text


def test_a_single_approval_fails_and_names_who_can_close_it(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "core1", "APPROVED")])
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "@core2" in verdict.requirements[0].who
    assert "@core1" not in verdict.requirements[0].who


def test_an_approval_from_a_stranger_does_not_count(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "core1", "APPROVED"), _review(qc, "passer-by", "APPROVED")])
    assert not _evaluate(qc, roster, pr).passed


def test_an_approval_given_before_the_last_push_does_not_count(qc: ModuleType, roster) -> None:
    pr = _pr(
        qc,
        reviews=[_review(qc, "core1", "APPROVED", sha="0" * 40), _review(qc, "comm1", "APPROVED")],
    )
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert any("before the last push" in note for note in verdict.notes)


def test_an_approval_from_someone_who_pushed_to_the_branch_does_not_count(qc: ModuleType, roster) -> None:
    pr = _pr(
        qc,
        reviews=[_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")],
        contributors={"outsider", "comm1"},
    )
    assert not _evaluate(qc, roster, pr).passed


def test_changes_requested_blocks_even_with_a_full_quorum(qc: ModuleType, roster) -> None:
    pr = _pr(
        qc,
        reviews=[
            _review(qc, "core1", "APPROVED"),
            _review(qc, "comm1", "APPROVED"),
            _review(qc, "core2", "CHANGES_REQUESTED"),
        ],
    )
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "changes requested" in verdict.requirements[0].text


def test_a_comment_does_not_replace_an_earlier_verdict(qc: ModuleType, roster) -> None:
    # GitHub keeps an approval standing through a later comment-only review.
    pr = _pr(
        qc,
        reviews=[
            _review(qc, "core1", "APPROVED", at="2026-09-10T10:00:00Z"),
            _review(qc, "core1", "COMMENTED", at="2026-09-10T11:00:00Z"),
            _review(qc, "comm1", "APPROVED"),
        ],
    )
    assert _evaluate(qc, roster, pr).passed


def test_a_later_approval_replaces_an_earlier_request_for_changes(qc: ModuleType, roster) -> None:
    pr = _pr(
        qc,
        reviews=[
            _review(qc, "core1", "CHANGES_REQUESTED", at="2026-09-10T09:00:00Z"),
            _review(qc, "core1", "APPROVED", at="2026-09-10T11:00:00Z"),
            _review(qc, "comm1", "APPROVED"),
        ],
    )
    assert _evaluate(qc, roster, pr).passed


# --- size and sensitivity (charter section 3) -------------------------------


def test_over_four_hundred_lines_needs_a_third_approval_from_a_second_core(qc: ModuleType, roster) -> None:
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED"), _review(qc, "comm2", "APPROVED")]
    pr = _pr(qc, changed_lines=401, reviews=reviews)
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    reviews.append(_review(qc, "core2", "APPROVED"))
    assert _evaluate(qc, roster, _pr(qc, changed_lines=401, reviews=reviews)).passed


def test_a_sensitive_path_needs_the_third_approval_at_any_size(qc: ModuleType, roster) -> None:
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")]
    pr = _pr(qc, changed_lines=12, paths=["src/bernstein/core/security/auth.py"], reviews=reviews)
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "security" in verdict.requirements[0].text


def test_over_a_thousand_lines_needs_the_maintainer(qc: ModuleType, roster) -> None:
    reviews = [
        _review(qc, "core1", "APPROVED"),
        _review(qc, "core2", "APPROVED"),
        _review(qc, "comm1", "APPROVED"),
    ]
    verdict = _evaluate(qc, roster, _pr(qc, changed_lines=1001, reviews=reviews))
    assert not verdict.passed
    assert any("maintainer approval or a split" in req.text for req in verdict.requirements)


def test_a_protected_path_needs_its_owner(qc: ModuleType, roster) -> None:
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "core2", "APPROVED")]
    verdict = _evaluate(qc, roster, _pr(qc, paths=["src/bernstein/core/tasks/claim.py"], reviews=reviews))
    assert not verdict.passed
    assert any("owner of" in req.text and "@owner" in req.who for req in verdict.requirements)


def test_the_catch_all_owner_rule_is_not_treated_as_a_protected_path(qc: ModuleType, roster) -> None:
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")]
    verdict = _evaluate(qc, roster, _pr(qc, paths=["README.md"], reviews=reviews))
    assert verdict.passed


# --- automation, the workflow account and the maintainer --------------------


def test_automation_merges_its_own_ordinary_change(qc: ModuleType, roster) -> None:
    verdict = _evaluate(qc, roster, _pr(qc, author="the-conductor[bot]", contributors=set()))
    assert verdict.passed
    assert any("section 1" in note for note in verdict.notes)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "schemas/task.json",
        "proto/tasks.proto",
        "src/bernstein/core/security/auth.py",
        "tests/integration/test_sandbox_egress.py",
    ],
)
def test_automation_needs_the_maintainer_on_a_path_it_must_not_land_alone(qc: ModuleType, roster, path: str) -> None:
    pr = _pr(qc, author="renovate[bot]", paths=[path], contributors=set())
    assert not _evaluate(qc, roster, pr).passed
    approved = _pr(
        qc,
        author="renovate[bot]",
        paths=[path],
        contributors=set(),
        reviews=[_review(qc, "owner", "APPROVED")],
    )
    assert _evaluate(qc, roster, approved).passed


def test_a_pull_request_from_the_workflow_account_needs_the_maintainer(qc: ModuleType, roster) -> None:
    pr = _pr(qc, author="github-actions[bot]", contributors=set())
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "workflow account" in verdict.requirements[0].text


def test_the_maintainers_own_change_merges_without_approvals(qc: ModuleType, roster) -> None:
    assert _evaluate(qc, roster, _pr(qc, author="owner", contributors={"owner"})).passed


def test_a_committer_can_block_the_maintainer(qc: ModuleType, roster) -> None:
    pr = _pr(qc, author="owner", contributors={"owner"}, reviews=[_review(qc, "comm1", "CHANGES_REQUESTED")])
    assert not _evaluate(qc, roster, pr).passed


def test_a_stranger_cannot_block_the_maintainer(qc: ModuleType, roster) -> None:
    pr = _pr(qc, author="owner", contributors={"owner"}, reviews=[_review(qc, "passer-by", "CHANGES_REQUESTED")])
    assert _evaluate(qc, roster, pr).passed


# --- the objection window on governance files (charter section 10) ----------


def test_a_governance_change_waits_seventy_two_hours(qc: ModuleType, roster) -> None:
    pr = _pr(qc, author="owner", contributors={"owner"}, paths=["GOVERNANCE.md"], pushed_hours_ago=1)
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "71h from now" in verdict.requirements[0].who


def test_a_governance_change_merges_once_the_window_closes(qc: ModuleType, roster) -> None:
    pr = _pr(qc, author="owner", contributors={"owner"}, paths=["GOVERNANCE.md"], pushed_hours_ago=73)
    assert _evaluate(qc, roster, pr).passed


def test_the_window_applies_to_the_roster_and_to_this_script(qc: ModuleType, roster) -> None:
    for path in (".github/quorum-roster.toml", "scripts/quorum_check.py"):
        pr = _pr(qc, author="owner", contributors={"owner"}, paths=[path], pushed_hours_ago=2)
        assert not _evaluate(qc, roster, pr).passed, path


# --- drafts and plumbing ----------------------------------------------------


def test_a_draft_is_neutral(qc: ModuleType, roster) -> None:
    verdict = _evaluate(qc, roster, _pr(qc, is_draft=True))
    assert verdict.passed
    assert verdict.requirements == []


def test_the_summary_names_the_charter_and_the_roster(qc: ModuleType, roster) -> None:
    summary = _evaluate(qc, roster, _pr(qc)).summary()
    assert "review-charter.md" in summary and "quorum-roster.toml" in summary
    assert "| Requirement | Met | Who can satisfy it |" in summary


def test_the_merge_group_ref_names_the_queued_pull_request(qc: ModuleType) -> None:
    env = {
        "GITHUB_EVENT_NAME": "merge_group",
        "GITHUB_REF": "refs/heads/gh-readonly-queue/main/pr-5737-d45d2c3a9461916133c746dc91749d7f824706a6",
    }
    assert qc.pr_number_from_env(env) == 5737


def test_no_pull_request_in_the_event_is_not_a_failure(qc: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # The variables have to go: this suite also runs inside Actions, where a
    # real pull request event would otherwise be picked up and called.
    for name in ("GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "GITHUB_REF", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
    assert qc.main(["--repo", "o/r", "--root", str(REPO_ROOT)]) == 0


def test_co_authors_are_read_from_the_commit_trailer(qc: ModuleType) -> None:
    commit = {
        "author": {"login": "outsider"},
        "committer": None,
        "commit": {"message": "fix: thing\n\nCo-authored-by: Some One <12345+core1@users.noreply.github.com>"},
    }
    assert qc._logins_from_commit(commit) == {"outsider", "core1"}


def test_the_roster_and_codeowners_on_this_branch_load(qc: ModuleType) -> None:
    # Guards the shipped files themselves: a typo in either is a broken check.
    loaded = qc.load_roster(str(REPO_ROOT))
    assert loaded.maintainer
    assert loaded.core_reviewers and loaded.committers
    owners = qc.load_codeowners(str(REPO_ROOT))
    assert owners and owners[0][0] == "*"
    assert qc.owners_for(".github/workflows/ci.yml", owners) == ([loaded.maintainer], True)


def test_annotation_names_the_first_unmet_requirement(qc: ModuleType) -> None:
    verdict = qc.Verdict(passed=False, title="2 requirement(s) missing")
    verdict.requirements.append(
        qc.Requirement("2 approvals, 1 from a core reviewer (40 changed lines)", False, "@a, @b")
    )
    verdict.requirements.append(qc.Requirement("approval from the owner of `x.py`", False, "@m"))
    assert qc.annotation(verdict) == (
        "waiting for: 2 approvals, 1 from a core reviewer (40 changed lines) - @a, @b (+1 more in the job summary)"
    )


def test_annotation_strips_backticks_and_handles_a_single_gap(qc: ModuleType) -> None:
    verdict = qc.Verdict(passed=False, title="1 requirement(s) missing")
    verdict.requirements.append(
        qc.Requirement("72 hours open for objections (touches `GOVERNANCE.md`)", False, "nobody")
    )
    assert qc.annotation(verdict) == "waiting for: 72 hours open for objections (touches GOVERNANCE.md) - nobody"
