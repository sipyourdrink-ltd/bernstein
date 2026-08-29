"""A pull request is described from its change, not from the session (#4484).

The run that implemented per-store key derivation opened its pull request
titled ``fix: resolve lint gate failures — SIM108 ternary operator and F841
unused variable``, because the title came from the run's newest commit and the
run happened to end with a lint repair. The body opened with the session's own
status text. Reviewers and the changelog both saw a lint cleanup; the feature
was invisible.

Each test below is named for the property it protects: which commit may name a
pull request, what the body may contain, and whether a reader can check that a
description belongs to the diff it claims to describe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.integrations.pr_gen import (
    COMMIT_LOG_FORMAT,
    ChangeProvenance,
    CommitRecord,
    CostBreakdown,
    FileChange,
    GateResult,
    SessionSummary,
    attest_pr_description,
    build_pr_body,
    build_pr_title,
    build_provenance,
    describe_commit,
    dominant_commit,
    is_housekeeping_commit,
    load_session_summary,
    parse_commit_log,
    rank_commits,
)
from bernstein.core.integrations.tickets import TicketPayload
from bernstein.core.review.receipt import verify_review_receipt

if TYPE_CHECKING:
    from collections.abc import Iterable

ISSUE_TITLE = "Per-store key derivation reuses one key across every store"
ISSUE_BODY = (
    "## Problem\n"
    "\n"
    "Every store opens with the same key, so a leak of one store's key reads them all.\n"
    "\n"
    "## Proposal\n"
    "\n"
    "Derive a per-store key with HKDF-SHA256.\n"
)


def _commit(
    sha: str,
    subject: str,
    files: Iterable[tuple[str, int, int]] = (),
    *,
    is_merge: bool = False,
) -> CommitRecord:
    """Build a :class:`CommitRecord` from ``(path, added, removed)`` triples."""
    return CommitRecord(
        sha=sha,
        subject=subject,
        is_merge=is_merge,
        files=tuple(FileChange(path=path, added=added, removed=removed) for path, added, removed in files),
    )


FEATURE_COMMIT = _commit(
    "aaaaaaaaaaaa",
    "feat(storage): derive a per-store key with HKDF-SHA256",
    [("src/bernstein/core/storage/keys.py", 120, 4), ("tests/unit/test_store_keys.py", 90, 0)],
)
LINT_COMMIT = _commit(
    "bbbbbbbbbbbb",
    "fix: resolve lint gate failures — SIM108 ternary operator and F841 unused variable",
    [("src/bernstein/core/storage/keys.py", 6, 8)],
)
FORMAT_COMMIT = _commit(
    "cccccccccccc",
    "style: reformat storage package",
    [("src/bernstein/core/storage/store.py", 40, 40)],
)
WIP_COMMIT = _commit("dddddddddddd", "[WIP] key derivation", [("notes/scratch.md", 10, 0)])
MERGE_COMMIT = _commit("eeeeeeeeeeee", "Merge branch 'main' into agent/run-1", is_merge=True)
CONTEXT_SYNC_COMMIT = _commit("ffffffffffff", "docs: refresh agent context", [("AGENTS.md", 12, 3)])


def _summary(**overrides: object) -> SessionSummary:
    base: dict[str, object] = {
        "session_id": "abcdef1234567890",
        "goal": f"Resolve GitHub issue #4484: {ISSUE_TITLE}\n\nWork only inside this repository.",
        "branch": "agent/abcdef12",
        "base_branch": "main",
        "primary_role": "engineer",
        "diff_stat": " src/bernstein/core/storage/keys.py | 124 +++++\n 1 file changed",
        "gates": (
            GateResult(name="lint", passed=True, detail="ruff: 0 findings"),
            GateResult(name="tests", passed=True, detail="pytest: 812 passed"),
        ),
        "cost": CostBreakdown(total_usd=1.0, total_tokens=1000, by_role={}),
    }
    base.update(overrides)
    return SessionSummary(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Which commit may name a pull request
# ---------------------------------------------------------------------------


def test_a_lint_repair_at_the_tip_does_not_title_the_pull_request() -> None:
    """The incident: the run's newest commit was a lint repair, not the change."""
    title = build_pr_title(ISSUE_TITLE, "engineer", ("bug",), commits=(LINT_COMMIT, FEATURE_COMMIT))

    assert "HKDF-SHA256" in title
    assert "lint" not in title.lower()


def test_a_formatting_commit_at_the_tip_does_not_title_the_pull_request() -> None:
    """A ``style:`` pass changes many lines and describes none of them."""
    title = build_pr_title(ISSUE_TITLE, "engineer", (), commits=(FORMAT_COMMIT, FEATURE_COMMIT))

    assert "HKDF-SHA256" in title
    assert "reformat" not in title.lower()


def test_an_all_housekeeping_run_falls_back_to_the_issue_title() -> None:
    """No substantive commit means no commit may name the pull request."""
    title = build_pr_title(ISSUE_TITLE, "engineer", (), commits=(LINT_COMMIT, FORMAT_COMMIT, MERGE_COMMIT))

    assert "per-store key derivation" in title.lower()
    assert "lint" not in title.lower()


def test_the_dominant_commit_is_the_one_that_changed_src_most() -> None:
    """Ranking is by ``src/`` churn, not by recency or by whole-tree size."""
    small_feature = _commit("111111111111", "feat: add a flag", [("src/a.py", 5, 0)])
    big_docs = _commit("222222222222", "docs: rewrite the guide", [("docs/guide.md", 900, 400)])

    assert dominant_commit((big_docs, small_feature, FEATURE_COMMIT)) == FEATURE_COMMIT


def test_a_docs_only_run_still_gets_a_real_title() -> None:
    """With no ``src/`` churn anywhere, whole-tree churn decides."""
    small = _commit("111111111111", "docs: fix a link", [("docs/a.md", 1, 1)])
    large = _commit("222222222222", "docs: rewrite the deployment guide", [("docs/b.md", 300, 90)])

    assert dominant_commit((small, large)) == large


@pytest.mark.parametrize(
    ("commit", "why"),
    [
        (MERGE_COMMIT, "merge commit"),
        (WIP_COMMIT, "work-in-progress marker over no source change"),
        (FORMAT_COMMIT, "style: type"),
        (LINT_COMMIT, "lint-repair wording under a fix: type"),
        (CONTEXT_SYNC_COMMIT, "generated agent-context file only"),
        (_commit("999999999999", "chore: bump the pin", [("uv.lock", 4, 4)]), "chore: type"),
        (_commit("888888888888", "fixup! feat: keys", [("src/a.py", 3, 0)]), "rebase marker"),
        (_commit("777777777777", "empty", []), "touches no files"),
    ],
)
def test_structural_housekeeping_never_names_a_pull_request(commit: CommitRecord, why: str) -> None:
    assert is_housekeeping_commit(commit), f"{why} should be housekeeping"


def test_a_substantive_commit_is_not_mistaken_for_housekeeping() -> None:
    assert not is_housekeeping_commit(FEATURE_COMMIT)


def test_ranking_is_deterministic_when_churn_ties() -> None:
    """Two equal commits must rank the same way on every run."""
    first = _commit("111111111111", "feat: one", [("src/a.py", 10, 0)])
    second = _commit("222222222222", "feat: two", [("src/b.py", 10, 0)])

    assert rank_commits((first, second)) == (first, second)
    assert rank_commits((second, first)) == (second, first)


def test_a_run_with_no_commits_still_titles_from_the_issue() -> None:
    """Nothing to rank is not the same as everything ranked away."""
    assert "per-store key derivation" in build_pr_title(ISSUE_TITLE, "engineer", ()).lower()


# ---------------------------------------------------------------------------
# Reading commits out of git
# ---------------------------------------------------------------------------


def test_commit_log_parsing_reads_shas_subjects_merges_and_churn() -> None:
    raw = (
        "\x1eaaa111\x1fbbb222 ccc333\x1fMerge branch 'main'\n"
        "\x1ebbb222\x1fddd444\x1ffeat(storage): derive a per-store key\n"
        "120\t4\tsrc/bernstein/core/storage/keys.py\n"
        "-\t-\ttests/fixtures/blob.bin\n"
    )

    parsed = parse_commit_log(raw)

    assert [c.sha for c in parsed] == ["aaa111", "bbb222"]
    assert parsed[0].is_merge is True
    assert parsed[1].is_merge is False
    assert parsed[1].subject == "feat(storage): derive a per-store key"
    assert parsed[1].src_churn == 124
    # A binary file counts as a touched path with no line churn.
    assert parsed[1].files[1] == FileChange(path="tests/fixtures/blob.bin", added=0, removed=0)


def test_commit_log_parsing_takes_the_destination_of_a_rename() -> None:
    raw = "\x1eaaa111\x1fbbb222\x1frefactor: move the module\n10\t2\tsrc/{old => new}/keys.py\n"

    assert parse_commit_log(raw)[0].files[0].path == "src/new/keys.py"


def test_the_commit_log_format_is_the_one_the_parser_reads() -> None:
    """Drift between the git invocation and the parser would empty the title."""
    assert COMMIT_LOG_FORMAT == "format:\x1e%H\x1f%P\x1f%s"


# ---------------------------------------------------------------------------
# What the body may contain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("banned", ["Automated session", "Completed:"])
@pytest.mark.parametrize(
    "summary",
    [
        _summary(),
        _summary(goal="", diff_stat="", gates=()),
        _summary(changes_summary="- Fix lint errors in run_receipt.py: Completed: Fix lint errors"),
        _summary(commits=(LINT_COMMIT, FEATURE_COMMIT)),
    ],
    ids=["typical", "empty-session", "status-laden-wrapup", "with-commits"],
)
def test_generated_bodies_never_carry_session_status_lines(summary: SessionSummary, banned: str) -> None:
    """The body answers "what does this diff do", never "how did the run go"."""
    assert banned not in build_pr_body(summary)


def test_the_change_section_names_the_dominant_commit_and_its_files() -> None:
    body = build_pr_body(_summary(commits=(LINT_COMMIT, FEATURE_COMMIT)))
    change = body.split("## Change", 1)[1].split("## Verification", 1)[0]

    assert "derive a per-store key with HKDF-SHA256" in change
    assert "src/bernstein/core/storage/keys.py" in change
    assert "+120 / -4" in change


def test_the_change_section_labels_housekeeping_rather_than_hiding_it() -> None:
    change = build_pr_body(_summary(commits=(LINT_COMMIT, FEATURE_COMMIT))).split("## Change", 1)[1]

    assert "Housekeeping" in change
    assert "SIM108" in change


def test_the_problem_section_comes_from_the_issue_not_the_run_instructions() -> None:
    body = build_pr_body(_summary(issue_problem=ISSUE_BODY))
    problem = body.split("## Problem", 1)[1].split("## Change", 1)[0]

    assert "a leak of one store's key reads them all" in problem
    assert "Work only inside this repository" not in problem


def test_the_problem_section_drops_the_standing_instructions_of_a_goal() -> None:
    """Without a linked issue the goal is the source, minus its brief."""
    problem = build_pr_body(_summary()).split("## Problem", 1)[1].split("## Change", 1)[0]

    assert ISSUE_TITLE in problem
    assert "Work only inside this repository" not in problem


def test_the_verification_section_reports_the_gates_that_ran() -> None:
    verification = build_pr_body(_summary()).split("## Verification", 1)[1]

    assert "lint" in verification
    assert "ruff: 0 findings" in verification
    assert "pytest: 812 passed" in verification


# ---------------------------------------------------------------------------
# Provenance: does this description belong to this diff?
# ---------------------------------------------------------------------------

DIFF = b"diff --git a/src/keys.py b/src/keys.py\n+derive()\n"
OTHER_DIFF = b"diff --git a/src/keys.py b/src/keys.py\n+something-else()\n"


def test_the_body_carries_the_diff_hash_and_the_journal_head() -> None:
    provenance = build_provenance(diff=DIFF, journal_head="sha256:deadbeef")
    body = build_pr_body(_summary(provenance=provenance, journal_head="sha256:deadbeef"))

    assert "## Provenance" in body
    assert provenance.diff_hash in body
    assert "sha256:deadbeef" in body


def test_the_diff_hash_is_the_one_the_verifier_recomputes() -> None:
    """One hashing path: the body publishes what ``review-receipt`` checks."""
    from bernstein.core.review.receipt import compute_diff_hash

    assert build_provenance(diff=DIFF).diff_hash == compute_diff_hash(DIFF)


def test_a_body_without_provenance_omits_the_block() -> None:
    assert "## Provenance" not in build_pr_body(_summary())


def _attest(workdir: Path, description: str, diff: bytes) -> None:
    attest_pr_description(
        workdir=workdir,
        pr_url="https://github.com/sipyourdrink-ltd/bernstein/pull/4485",
        repo="sipyourdrink-ltd/bernstein",
        issue_body=ISSUE_BODY,
        description=description,
        diff=diff,
        journal_head="sha256:deadbeef",
        task_id="T-1",
        timestamp=1_700_000_000,
        hmac_key=b"k" * 32,
    )


def test_review_receipt_verify_accepts_a_description_matching_its_diff(tmp_path: Path) -> None:
    provenance = build_provenance(diff=DIFF, journal_head="sha256:deadbeef")
    description = build_pr_body(_summary(provenance=provenance, journal_head="sha256:deadbeef"))
    _attest(tmp_path, description, DIFF)

    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=b"k" * 32,
        pr_url="https://github.com/sipyourdrink-ltd/bernstein/pull/4485",
        issue_body=ISSUE_BODY,
        diff=DIFF,
    )

    assert result.ok, result.reason


def test_review_receipt_verify_rejects_a_description_whose_diff_changed(tmp_path: Path) -> None:
    provenance = build_provenance(diff=DIFF, journal_head="sha256:deadbeef")
    description = build_pr_body(_summary(provenance=provenance, journal_head="sha256:deadbeef"))
    _attest(tmp_path, description, DIFF)

    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=b"k" * 32,
        pr_url="https://github.com/sipyourdrink-ltd/bernstein/pull/4485",
        issue_body=ISSUE_BODY,
        diff=OTHER_DIFF,
    )

    assert not result.ok
    assert "diff_hash mismatch" in result.reason


def test_the_journal_head_is_read_from_the_run_journal(tmp_path: Path) -> None:
    run_dir = tmp_path / ".sdd" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
    (run_dir / "journal.jsonl").write_text(
        json.dumps({"event": "run_started", "event_hash": "sha256:first"})
        + "\n"
        + json.dumps({"event": "run_finished", "event_hash": "sha256:last"})
        + "\n",
        encoding="utf-8",
    )

    assert load_session_summary(None, workdir=tmp_path).journal_head == "sha256:last"


# ---------------------------------------------------------------------------
# Explicit overrides
# ---------------------------------------------------------------------------


TICKET = TicketPayload(
    id="sipyourdrink-ltd/bernstein#4484",
    title=ISSUE_TITLE,
    description=ISSUE_BODY,
    labels=("bug",),
    url="https://github.com/sipyourdrink-ltd/bernstein/issues/4484",
    source="github",
)


def _run_pr(args: list[str], *, commits: tuple[CommitRecord, ...] = (LINT_COMMIT, FEATURE_COMMIT)) -> str:
    """Invoke ``bernstein pr --dry-run`` with git and the tracker stubbed."""
    summary = _summary(commits=commits, provenance=ChangeProvenance(diff_hash="sha256:abc", journal_head="sha256:h"))
    slug = MagicMock(stdout="sipyourdrink-ltd/bernstein\n")
    with (
        patch("bernstein.cli.commands.pr_cmd.load_session_summary", return_value=summary),
        patch("bernstein.cli.commands.pr_cmd._enrich_summary_with_git", side_effect=lambda s, _w: s),
        patch("bernstein.cli.commands.pr_cmd.fetch_ticket", return_value=TICKET),
        patch("bernstein.cli.commands.pr_cmd.shutil.which", return_value="/usr/bin/gh"),
        patch("bernstein.cli.commands.pr_cmd.subprocess.run", return_value=slug),
    ):
        return CliRunner().invoke(cli, ["pr", "--dry-run", *args]).output


def test_the_dominant_commit_titles_the_pull_request_end_to_end() -> None:
    out = _run_pr(["--issue", "4484"])

    assert "HKDF-SHA256" in out.splitlines()[0]
    assert "SIM108" not in out.splitlines()[0]


def test_explicit_title_overrides_the_dominant_commit() -> None:
    out = _run_pr(["--issue", "4484", "--title", "fix: something else"])

    assert out.splitlines()[0] == "Title: fix: something else"


def test_explicit_body_overrides_the_generated_description() -> None:
    out = _run_pr(["--issue", "4484", "--body", "hand-written body"])

    assert "hand-written body" in out
    assert "## Provenance" not in out
    # The issue link survives an overridden body: without it merging leaves the
    # issue open, which is not something --body should be able to switch off.
    assert "Closes #4484" in out


def test_body_is_a_registered_pr_option() -> None:
    from bernstein.cli.commands.pr_cmd import pr_cmd

    assert "--body" in {param.opts[0] for param in pr_cmd.params}


# ---------------------------------------------------------------------------
# What a description may claim when git does not answer
# ---------------------------------------------------------------------------


def test_a_git_failure_is_not_reported_as_a_branch_that_changed_nothing() -> None:
    """An unanswered query is not a negative answer.

    ``git diff`` returning non-zero used to be indistinguishable from a
    branch with no diff: both reached the body as an empty string, and the
    body asserted the branch changed nothing. Nine of the ten fleet pull
    requests open on 2026-08-27 said that about branches full of work.
    """
    body = build_pr_body(_summary(diff_stat="", commits=(), git_error="fatal: bad object main"))
    change = body.split("## Change", 1)[1].split("## Verification", 1)[0]

    assert "No changes recorded" not in change
    assert "could not be read" in change
    assert "fatal: bad object main" in change


def test_a_description_never_carries_a_receipt_for_an_empty_diff() -> None:
    """A digest of nothing is not provenance.

    ``compute_diff_hash(b"")`` is a well-formed SHA-256, and printing it
    beside ``review-receipt verify`` publishes a receipt no verifier can
    honour. The section is omitted instead.
    """
    empty = build_provenance(diff=b"")
    body = build_pr_body(_summary(provenance=empty))

    assert "## Provenance" not in body
    assert empty.diff_hash not in body


def test_a_receipt_for_a_real_diff_is_still_published() -> None:
    real = build_provenance(diff=b"diff --git a/x b/x\n+one line\n")
    body = build_pr_body(_summary(provenance=real))

    assert "## Provenance" in body
    assert real.diff_hash in body


def test_git_failures_reach_the_description_instead_of_being_swallowed(tmp_path: Path) -> None:
    """Proven against real git, in a directory that is not a repository.

    A mocked failure would prove the branch is written; only a real
    non-zero git proves the failure survives the subprocess boundary.
    """
    from bernstein.cli.commands.pr_cmd import _enrich_summary_with_git

    enriched = _enrich_summary_with_git(
        _summary(diff_stat="", commits=(), provenance=None, branch="some-branch"),
        tmp_path,
    )

    assert enriched.git_error
    assert enriched.provenance is None
    assert "No changes recorded" not in build_pr_body(enriched)


# ---------------------------------------------------------------------------
# What a reader sees first
# ---------------------------------------------------------------------------


def test_a_colon_lead_in_absorbs_the_block_it_introduces() -> None:
    """ "Two release tracks, going forward:" is the sentence before a problem."""
    body = build_pr_body(
        _summary(issue_problem=("Two release tracks, going forward:\n\n- stable ships monthly\n- edge ships nightly\n"))
    )
    problem = body.split("## Problem", 1)[1].split("## Change", 1)[0]

    assert "stable ships monthly" in problem
    assert "edge ships nightly" in problem


def test_the_body_opens_with_the_size_and_the_gates() -> None:
    body = build_pr_body(_summary(commits=(FEATURE_COMMIT,)))
    headline = body.splitlines()[0]

    assert headline.startswith(">")
    assert "2 files" in headline
    assert "+210 / -4" in headline
    assert "2/2 gates passed" in headline


def test_a_failed_gate_shows_in_the_headline() -> None:
    body = build_pr_body(_summary(gates=(GateResult(name="tests", passed=False, detail="3 failed"),)))
    headline = body.splitlines()[0]

    assert "❌" in headline
    assert "0/1 gates passed" in headline


def test_a_run_without_a_journal_head_does_not_advertise_one() -> None:
    """``Journal head: unrecorded`` reads as a lost recording, not an absent one."""
    body = build_pr_body(_summary(provenance=build_provenance(diff=b"diff --git a/x b/x\n")))
    provenance = body.split("## Provenance", 1)[1].split("\n---", 1)[0]

    assert "unrecorded" not in provenance
    assert "Journal head" not in provenance


def test_a_journal_head_is_shown_when_the_run_anchored_one() -> None:
    body = build_pr_body(_summary(provenance=build_provenance(diff=b"diff --git a/x b/x\n", journal_head="deadbeef")))

    assert "**Journal head:** `deadbeef`" in body


def test_the_body_states_no_spend_anywhere() -> None:
    """What a run cost describes the run, not the diff a reviewer is reading.

    The module already refuses to source the description from the session's
    own status text for exactly this reason. Spend was the one place that
    rule was not applied: it reached the headline as ``$38.94`` and the body
    as a per-role table, on a page anyone can read.
    """
    body = build_pr_body(
        _summary(commits=(FEATURE_COMMIT,), cost=CostBreakdown(total_usd=38.94, total_tokens=1_000_000))
    )

    assert "$" not in body
    assert "## Cost" not in body
    assert "Effective rate" not in body
    assert "38.94" not in body


def test_a_session_with_no_cost_recorded_renders_the_same_body() -> None:
    """No branch on spend survives, so the two bodies cannot drift apart."""
    priced = build_pr_body(_summary(commits=(FEATURE_COMMIT,), cost=CostBreakdown(total_usd=12.5, total_tokens=999)))
    free = build_pr_body(_summary(commits=(FEATURE_COMMIT,), cost=CostBreakdown()))

    assert priced == free


# ---------------------------------------------------------------------------
# A WIP marker is not a claim about what the commit did
# ---------------------------------------------------------------------------


WIP_FEATURE_COMMIT = _commit(
    "dddddddddddd",
    "[WIP] feat(review): add ApprovalBinding data model and emit/verify functions",
    files=(("src/bernstein/core/review/receipt.py", 227, 0),),
)
TEST_FIX_COMMIT = _commit(
    "ffffffffffff",
    "fix(tests): detect real openai-agents SDK in _sdk_installed()",
    files=(("tests/integration/adapters/test_openai_agents_smoke.py", 23, 1),),
)


def test_a_wip_commit_that_adds_source_can_still_name_the_pull_request() -> None:
    """The incident: 227 lines of new source were filed under housekeeping.

    Folding an agent worktree in commits substantive work behind a ``[WIP]``
    prefix. Classifying on the marker dropped that commit from the ranking
    entirely, so the description named the branch after a test fix that came
    after it and listed the feature under "not what this pull request is
    about" - while the header's own file count said six files and 300 lines.
    """
    assert is_housekeeping_commit(WIP_FEATURE_COMMIT) is False

    ranked = rank_commits((TEST_FIX_COMMIT, WIP_FEATURE_COMMIT))

    assert ranked[0] is WIP_FEATURE_COMMIT


def test_a_wip_commit_that_touches_no_source_is_still_housekeeping() -> None:
    """The marker keeps its meaning for the checkpoints it was written for."""
    assert is_housekeeping_commit(WIP_COMMIT) is True


def test_rebase_markers_are_housekeeping_whatever_they_touch() -> None:
    """``fixup!`` names a commit destined to be squashed into another one.

    Unlike ``[WIP]`` it says what will happen to the commit, not when it was
    written, so churn does not redeem it.
    """
    fixup = _commit(
        "cccccccccccc",
        "fixup! feat(storage): derive a per-store key with HKDF-SHA256",
        files=(("src/bernstein/core/storage/keys.py", 140, 12),),
    )

    assert is_housekeeping_commit(fixup) is True


# ---------------------------------------------------------------------------
# Uninformative subjects are described, not quoted (#4766)
# ---------------------------------------------------------------------------

WIP_FOLD_IN_COMMIT = _commit(
    "dddddddddddd",
    "[WIP] backend-3c1d21a5 partial work",
    [
        ("src/bernstein/core/agents/agent_lifecycle.py", 100, 5),
        ("src/bernstein/core/agents/in_process_agent.py", 20, 3),
    ],
)
WIP_BUT_DESCRIPTIVE_COMMIT = _commit(
    "eeeeeeeeeeee",
    "[WIP] rework the lease reaper's expiry sweep",
    [("src/bernstein/core/agents/reaper.py", 40, 10)],
)


def test_an_uninformative_wip_subject_renders_as_what_it_changed() -> None:
    """A squash merge copies the body onto main, so the session id became the
    permanent description of the change. Describe the diff instead."""
    rendered = describe_commit(WIP_FOLD_IN_COMMIT)
    assert "backend-3c1d21a5" not in rendered
    assert "partial work" not in rendered
    assert "src/bernstein/core/agents" in rendered
    assert "+120 / -8" in rendered


def test_a_wip_commit_with_a_real_subject_renders_unchanged() -> None:
    """The author's own words beat anything derived from a diff."""
    assert describe_commit(WIP_BUT_DESCRIPTIVE_COMMIT) == "[WIP] rework the lease reaper's expiry sweep"


def test_a_descriptive_subject_renders_unchanged() -> None:
    assert describe_commit(FEATURE_COMMIT) == FEATURE_COMMIT.subject


def test_a_single_file_uninformative_commit_names_the_file() -> None:
    one = _commit("ffffffffffff", "[WIP] qa-9 partial work", [("src/bernstein/core/x.py", 7, 2)])
    assert describe_commit(one) == "work in `src/bernstein/core/x.py` (+7 / -2)"


def test_an_uninformative_commit_touching_nothing_says_so() -> None:
    """Falling back to the subject here is what leaked the identifier."""
    empty = _commit("111111111111", "[WIP] backend-77 partial work")
    rendered = describe_commit(empty)
    assert "backend-77" not in rendered
    assert rendered == "checkpoint, no file changes"


def test_files_sharing_no_directory_are_described_as_across_the_tree() -> None:
    spread = _commit(
        "222222222222",
        "[WIP] backend-5 partial work",
        [("src/a.py", 3, 1), ("docs/b.md", 2, 0)],
    )
    assert describe_commit(spread) == "work across the tree (2 files, +5 / -1)"


def test_the_rendered_body_never_carries_a_session_identifier() -> None:
    """The end-to-end shape from the issue: a branch carrying both kinds."""
    body = build_pr_body(_summary(commits=(WIP_FOLD_IN_COMMIT, WIP_BUT_DESCRIPTIVE_COMMIT)))
    assert "backend-3c1d21a5" not in body
    assert "partial work" not in body
    # The descriptive one survives verbatim in the same body.
    assert "rework the lease reaper's expiry sweep" in body


def test_ranking_is_untouched_by_the_rendering_change() -> None:
    """#4726's behaviour still holds: a WIP commit with src/ churn is
    eligible to name the pull request, and outranks a smaller one."""
    assert not is_housekeeping_commit(WIP_FOLD_IN_COMMIT)
    ranked = rank_commits((WIP_BUT_DESCRIPTIVE_COMMIT, WIP_FOLD_IN_COMMIT))
    assert ranked[0] is WIP_FOLD_IN_COMMIT
