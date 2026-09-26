"""Unit tests for ``scripts/quorum_check.py``.

Every rule is exercised against a constructed ``PullRequest`` through
``evaluate()``, which is the whole decision: the network calls sit in
``fetch_pull_request`` and are not part of what is being tested here, so a
failure names the rule that regressed rather than a broken subprocess chain.
The exception is the net diff: which approvals, contributors and pushes it
carries is worked out from API answers, so those cases run
``fetch_pull_request`` against a fake of the API and then ``evaluate()``.

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


def _review(
    qc: ModuleType, login: str, state: str, *, sha: str = HEAD, at: str = "2026-09-10T10:00:00Z", body: str = ""
):
    return qc.Review(login=login, state=state, commit_id=sha, submitted_at=at, body=body)


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


def test_a_dismissed_approval_does_not_revive_an_earlier_request_for_changes(qc: ModuleType, roster) -> None:
    # GitHub dismisses an approval as stale on the next push. The reviewer had
    # already withdrawn their request by approving; the dismissal must not
    # bring the request back.
    pr = _pr(
        qc,
        reviews=[
            _review(qc, "core1", "CHANGES_REQUESTED", at="2026-09-10T09:00:00Z"),
            _review(qc, "core1", "DISMISSED", at="2026-09-10T10:00:00Z"),
            _review(qc, "core2", "APPROVED"),
            _review(qc, "comm1", "APPROVED"),
        ],
    )
    assert _evaluate(qc, roster, pr).passed


def test_a_dismissed_request_for_changes_does_not_revive_an_earlier_approval(qc: ModuleType, roster) -> None:
    pr = _pr(
        qc,
        reviews=[
            _review(qc, "core1", "APPROVED", at="2026-09-10T09:00:00Z"),
            _review(qc, "core1", "DISMISSED", at="2026-09-10T10:00:00Z"),
            _review(qc, "comm1", "APPROVED"),
        ],
    )
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed


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


def test_a_test_path_that_merely_names_a_sensitive_word_does_not_escalate(qc: ModuleType, roster) -> None:
    """A conformance vector for the auditor is not a change to the auditor.

    The words are matched against the whole path, so `tests/conformance/auditor/`
    used to ask for a third approval. Adding a test cannot loosen the control
    it exercises, and the quorum for a two-approval change is unchanged.
    """
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")]
    paths = ["tests/conformance/auditor/test_policy_vectors.py"]
    verdict = _evaluate(qc, roster, _pr(qc, changed_lines=12, paths=paths, reviews=reviews))
    assert verdict.passed


def test_a_release_note_naming_a_sensitive_word_does_not_escalate(qc: ModuleType, roster) -> None:
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")]
    paths = ["docs/release-notes/fragments/5072-govern-audit-check-contract.md"]
    verdict = _evaluate(qc, roster, _pr(qc, changed_lines=8, paths=paths, reviews=reviews))
    assert verdict.passed


def test_the_implementation_still_escalates_when_a_test_rides_along(qc: ModuleType, roster) -> None:
    """The exemption is per path, not per pull request."""
    reviews = [_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")]
    paths = [
        "tests/conformance/auditor/test_policy_vectors.py",
        "src/bernstein/core/security/audit_chain.py",
    ]
    verdict = _evaluate(qc, roster, _pr(qc, changed_lines=12, paths=paths, reviews=reviews))
    assert not verdict.passed
    assert "audit_chain.py" in verdict.requirements[0].text


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


# --- a CODEOWNERS line naming several people (charter section 4) ------------

# The adapters line is shared between the maintainer and one core reviewer;
# the core line above it still names the maintainer alone.
SHARED_OWNERS = [*OWNERS, ("/src/bernstein/adapters/", ["owner", "core1"])]


def _owner_rows(verdict) -> list:
    return [req for req in verdict.requirements if req.text.startswith("approval from the owner of")]


def test_one_approval_from_any_owner_of_a_shared_line_satisfies_it(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "core1", "APPROVED"), _review(qc, "comm1", "APPROVED")])
    verdict = qc.evaluate(pr, roster, SHARED_OWNERS, NOW)
    (row,) = _owner_rows(verdict)
    assert row.met
    assert verdict.passed


def test_a_shared_line_without_an_approval_names_everyone_who_could_give_one(qc: ModuleType, roster) -> None:
    (row,) = _owner_rows(qc.evaluate(_pr(qc), roster, SHARED_OWNERS, NOW))
    assert not row.met
    assert row.who == "@core1, @owner"


def test_a_single_owner_line_reads_exactly_as_before(qc: ModuleType, roster) -> None:
    # The regression guarantee for the grouping: a line naming one person
    # produces the same row, word for word, as it did when every owner was
    # a requirement of their own.
    paths = [f"src/bernstein/core/{name}.py" for name in ("a", "b", "c", "d")]
    expected_text = "approval from the owner of `src/bernstein/core/a.py`"
    (row,) = _owner_rows(_evaluate(qc, roster, _pr(qc, paths=paths[:1])))
    assert (row.text, row.met, row.who) == (expected_text, False, "@owner")
    approved = _pr(qc, paths=paths[:1], reviews=[_review(qc, "owner", "APPROVED")])
    (row,) = _owner_rows(_evaluate(qc, roster, approved))
    assert (row.text, row.met, row.who) == (expected_text, True, "@owner")
    (row,) = _owner_rows(_evaluate(qc, roster, _pr(qc, paths=paths)))
    assert row.text == (
        "approval from the owner of `src/bernstein/core/a.py`, `src/bernstein/core/b.py`, "
        "`src/bernstein/core/c.py` and others"
    )


def test_an_owner_who_wrote_or_pushed_the_change_cannot_satisfy_its_line(qc: ModuleType, roster) -> None:
    # As the author.
    pr = _pr(qc, author="core1", reviews=[_review(qc, "core1", "APPROVED")])
    (row,) = _owner_rows(qc.evaluate(pr, roster, SHARED_OWNERS, NOW))
    assert not row.met and row.who == "@owner"
    # As someone who pushed to the branch.
    pr = _pr(qc, reviews=[_review(qc, "core1", "APPROVED")], contributors={"outsider", "core1"})
    (row,) = _owner_rows(qc.evaluate(pr, roster, SHARED_OWNERS, NOW))
    assert not row.met and row.who == "@owner"
    # When nobody named on the line is left, the table says so.
    pr = _pr(qc, author="core1", contributors={"core1", "owner"})
    (row,) = _owner_rows(qc.evaluate(pr, roster, SHARED_OWNERS, NOW))
    assert not row.met and row.who == "nobody: every owner wrote or pushed this change"


def test_paths_are_grouped_by_the_owner_set_of_their_line(qc: ModuleType, roster) -> None:
    paths = ["src/bernstein/adapters/a.py", "src/bernstein/adapters/b.py", "src/bernstein/core/c.py"]
    rows = _owner_rows(qc.evaluate(_pr(qc, paths=paths), roster, SHARED_OWNERS, NOW))
    assert [(row.text, row.who) for row in rows] == [
        ("approval from the owner of `src/bernstein/adapters/a.py`, `src/bernstein/adapters/b.py`", "@core1, @owner"),
        ("approval from the owner of `src/bernstein/core/c.py`", "@owner"),
    ]


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


# --- adopted pull requests (charter section 3, until 2026-10-05) -------------


def _adopted(qc: ModuleType, **overrides):
    """An ordinary contributor change the maintainer has pushed to and declared adopted."""
    fields = dict(
        changed_lines=100,
        paths=["src/bernstein/cli/run.py"],
        contributors={"outsider", "owner"},
        reviews=[_review(qc, "owner", "COMMENTED", body=qc.ADOPTION_NOTICE)],
    )
    fields.update(overrides)
    return _pr(qc, **fields)


def test_an_adopted_change_merges_without_approvals(qc: ModuleType, roster) -> None:
    verdict = _evaluate(qc, roster, _adopted(qc))
    assert verdict.passed
    assert any("Adopted by the maintainer" in note for note in verdict.notes)


def test_the_notice_alone_does_not_adopt_a_change_the_maintainer_never_pushed_to(qc: ModuleType, roster) -> None:
    verdict = _evaluate(qc, roster, _adopted(qc, contributors={"outsider"}))
    assert not verdict.passed
    assert "2 approvals" in verdict.requirements[0].text


def test_a_push_alone_does_not_adopt_and_the_summary_says_what_would(qc: ModuleType, roster) -> None:
    verdict = _evaluate(qc, roster, _adopted(qc, reviews=[]))
    assert not verdict.passed
    assert any(qc.ADOPTION_NOTICE in note for note in verdict.notes)


def test_a_notice_given_before_the_last_push_does_not_adopt(qc: ModuleType, roster) -> None:
    stale = [_review(qc, "owner", "COMMENTED", sha="e" * 40, body=qc.ADOPTION_NOTICE)]
    assert not _evaluate(qc, roster, _adopted(qc, reviews=stale)).passed


def test_a_committer_can_block_an_adopted_change(qc: ModuleType, roster) -> None:
    reviews = [
        _review(qc, "owner", "COMMENTED", body=qc.ADOPTION_NOTICE),
        _review(qc, "comm1", "CHANGES_REQUESTED"),
    ]
    assert not _evaluate(qc, roster, _adopted(qc, reviews=reviews)).passed


def test_adoption_ends_on_its_date(qc: ModuleType, roster) -> None:
    after = qc.ADOPTION_ENDS + timedelta(seconds=1)
    verdict = qc.evaluate(_adopted(qc), roster, OWNERS, after)
    assert not verdict.passed
    assert not any("dopted" in note for note in verdict.notes)


@pytest.mark.parametrize(
    "overrides",
    [
        {"changed_lines": 401},
        # Sensitive, and outside every CODEOWNERS line of the fixture, so the
        # tier alone refuses it.
        {"paths": ["src/bernstein/plugins/sandbox_runner.py"]},
    ],
    ids=["large", "sensitive"],
)
def test_adoption_is_ignored_on_the_escalated_tier(qc: ModuleType, roster, overrides) -> None:
    verdict = _evaluate(qc, roster, _adopted(qc, **overrides))
    assert not verdict.passed
    assert "3 approvals" in verdict.requirements[0].text
    assert not any("dopted" in note or qc.ADOPTION_NOTICE in note for note in verdict.notes)


def test_adoption_is_ignored_on_a_codeowners_path(qc: ModuleType, roster) -> None:
    verdict = _evaluate(qc, roster, _adopted(qc, paths=["src/bernstein/core/tasks/claim.py"]))
    assert not verdict.passed
    assert not any("dopted" in note for note in verdict.notes)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "schemas/task.json", "proto/tasks.proto"])
def test_adoption_is_ignored_under_the_paths_automation_cannot_land_alone(qc: ModuleType, roster, path: str) -> None:
    # No CODEOWNERS line of its own here, so only the path class can refuse it.
    unowned = [("*", ["core1", "core2"])]
    verdict = qc.evaluate(_adopted(qc, paths=[path]), roster, unowned, NOW)
    assert not verdict.passed
    assert not any("dopted" in note for note in verdict.notes)


def test_adoption_is_honoured_on_an_ordinary_unowned_change(qc: ModuleType, roster) -> None:
    unowned = [("*", ["core1", "core2"])]
    verdict = qc.evaluate(_adopted(qc, paths=["src/bernstein/cli/run.py", "docs/guide.md"]), roster, unowned, NOW)
    assert verdict.passed
    assert any("Adopted by the maintainer" in note for note in verdict.notes)


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


# --- the backlog window (charter section 3, until 2026-10-05) ---------------


def test_the_maintainers_approval_alone_passes_an_ordinary_change_in_the_window(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "owner", "APPROVED")])
    verdict = _evaluate(qc, roster, pr)
    assert verdict.passed
    assert "2026-10-05" in verdict.requirements[0].text


def test_an_unapproved_change_in_the_window_names_the_maintainer_as_a_way_out(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "core1", "APPROVED")])
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "or @owner alone" in verdict.requirements[0].who


def test_the_window_does_not_lift_the_third_approval_on_a_large_change(qc: ModuleType, roster) -> None:
    pr = _pr(qc, changed_lines=401, reviews=[_review(qc, "owner", "APPROVED")])
    assert not _evaluate(qc, roster, pr).passed


def test_the_window_does_not_lift_the_third_approval_on_a_sensitive_path(qc: ModuleType, roster) -> None:
    pr = _pr(qc, paths=["src/bernstein/core/security/auth.py"], reviews=[_review(qc, "owner", "APPROVED")])
    assert not _evaluate(qc, roster, pr).passed


def test_the_window_closes_on_its_date(qc: ModuleType, roster) -> None:
    pr = _pr(qc, reviews=[_review(qc, "owner", "APPROVED")])
    after = qc.BACKLOG_WINDOW_ENDS + timedelta(minutes=1)
    verdict = qc.evaluate(pr, roster, OWNERS, after)
    assert not verdict.passed
    assert "2026-10-05" not in verdict.requirements[0].text


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
    assert not loaded.core_reviewers & loaded.committers  # a person is in exactly one list
    owners = qc.load_codeowners(str(REPO_ROOT))
    assert owners and all(pattern != "*" for pattern, _ in owners)  # no catch-all, on purpose
    assert qc.owners_for(".github/workflows/ci.yml", owners) == ([loaded.maintainer], True)
    assert qc.owners_for("README.md", owners) == ([], False)
    # core/ and adapters/ are shared with every core reviewer, and the four
    # sensitive subpackages under core/ stay with the maintainer alone.
    for path in ("src/bernstein/core/tasks/claim.py", "src/bernstein/adapters/aider.py"):
        shared, specific = qc.owners_for(path, owners)
        assert specific and set(shared) == set(loaded.core), path
    for sub in ("security", "identity", "sandbox", "tokens"):
        assert qc.owners_for(f"src/bernstein/core/{sub}/x.py", owners) == ([loaded.maintainer], True), sub


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


# --- the net diff: approvals, contributors and the window follow it ---------
#
# These cases drive ``fetch_pull_request`` through a fake of the GitHub API, so
# the fingerprint, the cache and the fail-closed fallbacks are exercised with
# the decision they feed rather than in isolation.

REPO = "o/r"
MAIN_TIP = "f" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_E = "e" * 40
CHANGED_FILE = "src/bernstein/cli/run.py"


def _when(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _patch(start: int, added: str = "+    retries = 3") -> str:
    """One hunk; ``start`` moves when the base branch shifts the file under it."""
    return f"@@ -{start},2 +{start},3 @@ def run(task):\n     plan = load(task)\n{added}\n     return execute(plan)"


def _raw_commit(
    sha: str,
    author: str,
    *,
    committer: str | None = None,
    parents: tuple[str, ...] = (MAIN_TIP,),
    hours_ago: float = 5.0,
    message: str = "change",
) -> dict:
    return {
        "sha": sha,
        "parents": [{"sha": parent} for parent in parents],
        "author": {"login": author},
        "committer": {"login": committer or author},
        "commit": {"message": message, "committer": {"date": _when(hours_ago)}},
    }


def _compare(commits: list[dict], patch: str | None = None, *, files: list[dict] | None = None) -> dict:
    if files is None:
        files = [{"filename": CHANGED_FILE, "status": "modified", "patch": patch or _patch(10)}]
    return {"files": files, "commits": commits, "total_commits": len(commits)}


def _raw_review(login: str, sha: str, state: str = "APPROVED", body: str = "") -> dict:
    return {
        "user": {"login": login},
        "state": state,
        "commit_id": sha,
        "submitted_at": "2026-09-10T10:00:00Z",
        "body": body,
    }


class FakeGitHub:
    """Answers the calls ``fetch_pull_request`` makes, and records them.

    A compare for a sha it was not given fails the way a missing commit does,
    with a non-zero exit from ``gh``.
    """

    def __init__(
        self,
        *,
        commits: list[dict],
        compares: dict[str, object],
        reviews: list[dict] | None = None,
        force_pushes: list[tuple[float, str, str]] | None = None,
        author: str = "outsider",
        paths: list[str] | None = None,
    ) -> None:
        self.commits = commits
        self.compares = compares
        self.reviews = reviews or []
        self.force_pushes = force_pushes or []
        self.author = author
        self.paths = paths or [CHANGED_FILE]
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        import subprocess

        self.calls.append(args)
        endpoint = args[1]
        if endpoint == "graphql":
            nodes = [
                {"createdAt": _when(hours_ago), "beforeCommit": {"oid": before}, "afterCommit": {"oid": after}}
                for hours_ago, before, after in self.force_pushes
            ]
            timeline = {"totalCount": len(nodes), "nodes": nodes}
            return {"data": {"repository": {"pullRequest": {"timelineItems": timeline}}}}
        if endpoint == f"repos/{REPO}/pulls/1":
            return {
                "user": {"login": self.author},
                "draft": False,
                "head": {"sha": self.commits[-1]["sha"]},
                "base": {"ref": "main"},
                "additions": 60,
                "deletions": 40,
            }
        if endpoint == f"repos/{REPO}/pulls/1/files":
            return [{"filename": path} for path in self.paths]
        if endpoint == f"repos/{REPO}/pulls/1/reviews":
            return self.reviews
        if endpoint == f"repos/{REPO}/pulls/1/commits":
            return self.commits
        prefix = f"repos/{REPO}/compare/main..."
        if endpoint.startswith(prefix):
            answer = self.compares.get(endpoint[len(prefix) :])
            if answer is None:
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 404: Not Found")
            if isinstance(answer, BaseException):
                raise answer
            return answer
        raise AssertionError(f"unexpected call: {args}")

    def compare_calls(self) -> list[str]:
        return [args[1].rsplit("...", 1)[1] for args in self.calls if "/compare/" in args[1]]


def _fetch(qc: ModuleType, fake: FakeGitHub):
    return qc.fetch_pull_request(REPO, 1, api=fake)


def _merged_main(*, merger: str = "owner", neutral: bool = True, reviews: list[dict] | None = None) -> FakeGitHub:
    """The contributor's commit A, then ``merger`` merges ``main`` in as B."""
    commit_a = _raw_commit(SHA_A, "outsider", hours_ago=80)
    merge_b = _raw_commit(SHA_B, merger, parents=(SHA_A, SHA_E), hours_ago=1, message="Merge branch 'main'")
    return FakeGitHub(
        commits=[commit_a, merge_b],
        compares={
            SHA_A: _compare([commit_a], _patch(10)),
            # main grew four lines above the change: same diff, new line numbers.
            SHA_B: _compare([commit_a, merge_b], _patch(14) if neutral else _patch(14, "+    retries = 30")),
        },
        reviews=reviews,
    )


def test_the_fingerprint_ignores_line_numbers_and_nothing_else(qc: ModuleType) -> None:
    moved = qc.net_diff_fingerprint(_compare([], _patch(14)))
    assert moved is not None
    assert moved == qc.net_diff_fingerprint(_compare([], _patch(10)))
    assert moved != qc.net_diff_fingerprint(_compare([], _patch(10, "+    retries = 30")))
    renamed = [{"filename": CHANGED_FILE, "previous_filename": "run.py", "status": "renamed", "patch": _patch(10)}]
    assert moved != qc.net_diff_fingerprint(_compare([], files=renamed))
    tail = qc.net_diff_fingerprint(_compare([], _patch(10).replace("def run(task):", "def plan(task):")))
    assert moved != tail


def test_an_approval_survives_a_merge_of_main_that_leaves_the_diff_alone(qc: ModuleType, roster) -> None:
    fake = _merged_main(merger="outsider", reviews=[_raw_review("core1", SHA_A), _raw_review("comm1", SHA_A)])
    pr = _fetch(qc, fake)
    assert pr.head_sha == SHA_B
    assert SHA_A in pr.same_diff
    verdict = _evaluate(qc, roster, pr)
    assert verdict.passed
    assert not any("before the last push" in note for note in verdict.notes)


def test_an_approval_is_void_after_a_push_that_changes_the_diff(qc: ModuleType, roster) -> None:
    fake = _merged_main(
        merger="outsider", neutral=False, reviews=[_raw_review("core1", SHA_A), _raw_review("comm1", SHA_A)]
    )
    pr = _fetch(qc, fake)
    assert SHA_A not in pr.same_diff
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert any("before the last push" in note for note in verdict.notes)


@pytest.mark.parametrize(
    "broken",
    [
        _compare(
            [], files=[{"filename": f"f{i}.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x"} for i in range(300)]
        ),
        _compare([], files=[{"filename": "logo.png", "status": "modified"}]),
        ValueError("gh returned something that is not JSON"),
        None,  # the compare call itself fails
    ],
    ids=["truncated", "binary-or-too-large", "unreadable", "api-error"],
)
def test_an_uncomputable_fingerprint_falls_back_to_the_head_sha(qc: ModuleType, roster, broken) -> None:
    fake = _merged_main(merger="outsider", reviews=[_raw_review("core1", SHA_A), _raw_review("comm1", SHA_A)])
    if broken is None:
        del fake.compares[SHA_A]
    else:
        fake.compares[SHA_A] = broken
    pr = _fetch(qc, fake)
    assert pr.same_diff == frozenset()
    assert not _evaluate(qc, roster, pr).passed
    # The same approvals on the head itself still count: sha binding is the floor.
    fake.reviews = [_raw_review("core1", SHA_B), _raw_review("comm1", SHA_B)]
    assert _evaluate(qc, roster, _fetch(qc, fake)).passed


def test_each_sha_is_compared_at_most_once_per_run(qc: ModuleType) -> None:
    reviews = [_raw_review("core1", SHA_A), _raw_review("comm1", SHA_A), _raw_review("owner", SHA_A)]
    fake = _merged_main(reviews=reviews)
    _fetch(qc, fake)
    calls = fake.compare_calls()
    assert sorted(calls) == sorted(set(calls))


def test_a_maintainer_who_only_merged_main_can_still_approve(qc: ModuleType, roster) -> None:
    fake = _merged_main(reviews=[_raw_review("owner", SHA_B), _raw_review("comm1", SHA_B)])
    pr = _fetch(qc, fake)
    assert "owner" in pr.contributors  # still counts as having pushed, for adoption
    assert "owner" in pr.update_only
    assert _evaluate(qc, roster, pr).passed


def test_a_merge_of_main_that_changes_the_diff_keeps_the_maintainer_out(qc: ModuleType, roster) -> None:
    fake = _merged_main(neutral=False, reviews=[_raw_review("owner", SHA_B), _raw_review("comm1", SHA_B)])
    pr = _fetch(qc, fake)
    assert "owner" not in pr.update_only
    assert not _evaluate(qc, roster, pr).passed


def _rebased(*, before_committer: str = "outsider", neutral: bool = True, reviews=None) -> FakeGitHub:
    """The contributor's A, rebased by the maintainer onto a newer main as B."""
    commit_a = _raw_commit(SHA_A, "outsider", committer=before_committer, hours_ago=80)
    commit_b = _raw_commit(SHA_B, "outsider", committer="owner", parents=(SHA_E,), hours_ago=1)
    return FakeGitHub(
        commits=[commit_b],
        compares={
            SHA_A: _compare([commit_a], _patch(10)),
            SHA_B: _compare([commit_b], _patch(14) if neutral else _patch(14, "+    retries = 30")),
        },
        force_pushes=[(1, SHA_A, SHA_B)],
        reviews=reviews,
    )


def test_a_maintainer_whose_rebase_left_the_diff_alone_can_still_approve(qc: ModuleType, roster) -> None:
    pr = _fetch(qc, _rebased(reviews=[_raw_review("owner", SHA_B), _raw_review("comm1", SHA_B)]))
    assert "owner" in pr.update_only
    assert _evaluate(qc, roster, pr).passed


def test_a_rebase_that_changed_the_diff_keeps_the_maintainer_out(qc: ModuleType, roster) -> None:
    pr = _fetch(qc, _rebased(neutral=False, reviews=[_raw_review("owner", SHA_B), _raw_review("comm1", SHA_B)]))
    assert "owner" not in pr.update_only
    assert not _evaluate(qc, roster, pr).passed


def test_a_rebase_does_not_launder_content_the_rebaser_pushed_before_it(qc: ModuleType, roster) -> None:
    # The maintainer committed A under someone else's name, then rebased it:
    # the rewrite changed nothing, but what it rewrote was already theirs.
    fake = _rebased(before_committer="owner", reviews=[_raw_review("owner", SHA_B), _raw_review("comm1", SHA_B)])
    pr = _fetch(qc, fake)
    assert "owner" not in pr.update_only
    assert not _evaluate(qc, roster, pr).passed


def test_a_maintainer_content_commit_keeps_them_out(qc: ModuleType, roster) -> None:
    commit_a = _raw_commit(SHA_A, "outsider", hours_ago=80)
    commit_b = _raw_commit(SHA_B, "owner", parents=(SHA_A,), hours_ago=1)
    fake = FakeGitHub(
        commits=[commit_a, commit_b],
        compares={SHA_B: _compare([commit_a, commit_b], _patch(10, "+    retries = 30"))},
        reviews=[_raw_review("owner", SHA_B), _raw_review("comm1", SHA_B)],
    )
    pr = _fetch(qc, fake)
    assert "owner" not in pr.update_only
    assert not _evaluate(qc, roster, pr).passed


def _governance(fake: FakeGitHub) -> FakeGitHub:
    fake.author = "owner"
    fake.paths = ["GOVERNANCE.md"]
    return fake


def test_the_window_runs_from_the_last_push_that_changed_the_diff(qc: ModuleType, roster) -> None:
    # Content at 80h, a diff-neutral merge of main at 1h: the window is closed.
    pr = _fetch(qc, _governance(_merged_main()))
    assert pr.last_push == NOW - timedelta(hours=1)
    assert pr.diff_changed_at == NOW - timedelta(hours=80)
    assert _evaluate(qc, roster, pr).passed


def test_the_window_restarts_when_the_merge_changed_the_diff(qc: ModuleType, roster) -> None:
    pr = _fetch(qc, _governance(_merged_main(neutral=False)))
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "71h from now" in verdict.requirements[0].who


def test_the_window_runs_back_through_a_rebase_that_left_the_diff_alone(qc: ModuleType, roster) -> None:
    pr = _fetch(qc, _governance(_rebased()))
    assert pr.diff_changed_at == NOW - timedelta(hours=80)
    assert _evaluate(qc, roster, pr).passed


def test_the_window_falls_back_to_the_last_push_when_the_diff_cannot_be_compared(qc: ModuleType, roster) -> None:
    fake = _governance(_merged_main())
    del fake.compares[SHA_A]
    pr = _fetch(qc, fake)
    assert pr.diff_changed_at is None
    verdict = _evaluate(qc, roster, pr)
    assert not verdict.passed
    assert "71h from now" in verdict.requirements[0].who
