"""Tests for the shared caller-less-module ratchet helper (#5552).

Two layers, tested separately:

* :func:`describe_ratchet_drift` is a pure function over three sets --
  tested with synthetic input, no git involved. This is the message-shape
  contract every guard using the helper gets for free.
* :func:`resolve_branch_only_ref` / :func:`scan_at_ref` are real git
  plumbing. Mocking ``subprocess.run`` here would only prove the mock is
  wired correctly, not that the plumbing actually recovers the right tree,
  so these are exercised against a real, small git repository built in
  ``tmp_path`` with a real merge commit -- literally constructing the
  situation the issue asks for, rather than reasoning about what git ought
  to do.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from tests.unit._orphan_scan import (
    describe_ratchet_drift,
    resolve_branch_only_ref,
    scan_at_ref,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# describe_ratchet_drift: pure, synthetic
# ---------------------------------------------------------------------------


def test_no_message_when_current_matches_baseline() -> None:
    assert (
        describe_ratchet_drift(
            baseline=frozenset({"a", "b"}),
            current={"a", "b"},
            branch_only=None,
            guard_name="pkg/",
            wire_hint="wire it.",
        )
        is None
    )


def test_both_directions_reported_in_one_message() -> None:
    """The defect this helper exists to fix: a naive two-assert guard only
    ever shows one direction per run. One call must show both.
    """
    message = describe_ratchet_drift(
        baseline=frozenset({"a", "b"}),
        current={"a", "c"},  # b resolved, c newly appeared
        branch_only=None,
        guard_name="pkg/",
        wire_hint="wire it.",
    )
    assert message is not None
    assert "['c']" in message
    assert "['b']" in message


def test_branch_only_matching_baseline_names_the_default_branch() -> None:
    """AC1: a stale baseline states plainly that the branch is not at fault.

    Constructed directly: the branch's own tree is byte-for-byte what the
    baseline expects; only ``current`` (the merge result) has drifted, so
    the drift cannot have come from this branch's own commits.
    """
    message = describe_ratchet_drift(
        baseline=frozenset({"a", "b"}),
        current={"a", "b", "c"},
        branch_only={"a", "b"},
        guard_name="pkg/",
        wire_hint="wire it.",
    )
    assert message is not None
    assert "not from this change" in message
    assert "['c']" in message


def test_branch_only_diverging_from_baseline_names_the_branch() -> None:
    """AC3: a genuinely new entry introduced *by the branch* still fails,
    with a message distinct from the self-exculpating one above.
    """
    message = describe_ratchet_drift(
        baseline=frozenset({"a", "b"}),
        current={"a", "b", "c"},
        branch_only={"a", "b", "c"},
        guard_name="pkg/",
        wire_hint="wire it.",
    )
    assert message is not None
    assert "not from this change" not in message
    assert "belongs to this change" in message
    assert "['c']" in message


def test_missing_branch_signal_adds_no_attribution_claim() -> None:
    """``branch_only=None`` is "cannot tell", not "the branch did it"."""
    message = describe_ratchet_drift(
        baseline=frozenset({"a"}),
        current={"a", "b"},
        branch_only=None,
        guard_name="pkg/",
        wire_hint="wire it.",
    )
    assert message is not None
    assert "not from this change" not in message
    assert "belongs to this change" not in message


# ---------------------------------------------------------------------------
# resolve_branch_only_ref / scan_at_ref: real git, real merge commit
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def merged_repo(tmp_path: Path) -> Path:
    """Build a tiny repo whose ``HEAD`` is a real two-parent merge commit.

    History:

    1. ``main`` commit A: ``caller.py`` imports ``mod`` -- ``mod`` has a
       caller.
    2. A feature branch forks from A and adds an unrelated file. It never
       touches ``caller.py`` or ``mod``, so its own tree still matches A's
       reachability picture exactly.
    3. ``main`` commit B (after the fork) deletes ``caller.py`` -- ``mod``
       loses its only caller, entirely independent of the feature branch.
    4. ``main`` (at commit B) merges the feature branch in -- mirroring how
       a merge queue merges the PR branch into the up-to-date target branch
       -- producing a merge commit whose parent 2 is the feature branch's
       own pre-merge tip.

    ``HEAD`` afterwards models exactly the merge-queue scenario in #5552:
    the merge result shows ``mod`` as newly caller-less, but the feature
    branch's own tree (parent 2) does not.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)

    (repo / "caller.py").write_text("import mod\n", encoding="utf-8")
    (repo / "mod.py").write_text("X = 1\n", encoding="utf-8")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "A: mod has a caller"], repo)

    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "unrelated.py").write_text("Y = 2\n", encoding="utf-8")
    _run_git(["add", "unrelated.py"], repo)
    _run_git(["commit", "-q", "-m", "feature: unrelated addition"], repo)
    feature_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    _run_git(["checkout", "-q", "main"], repo)
    (repo / "caller.py").unlink()
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "B: unrelated drift removes mod's caller"], repo)

    # A merge queue merges the *PR branch* into the up-to-date target branch
    # (conceptually `git checkout main && git merge feature`), not the other
    # way around. Git's first-parent slot is always the branch checked out
    # before the merge, so that ordering -- not "feature merges main" -- is
    # what makes ``HEAD^2`` resolve to the PR branch's own tip below.
    _run_git(["checkout", "-q", "main"], repo)
    _run_git(["merge", "-q", "--no-ff", "-m", "merge feature into main", "feature"], repo)

    merge_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    parents = subprocess.run(
        ["git", "rev-parse", "HEAD^1", "HEAD^2"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.split()
    assert merge_head not in (feature_tip,)
    assert parents[1] == feature_tip, "HEAD^2 must be the feature branch's own pre-merge tip"

    return repo


def test_resolve_branch_only_ref_finds_the_second_parent(merged_repo: Path) -> None:
    branch_ref = resolve_branch_only_ref(merged_repo)
    assert branch_ref is not None

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD^2"], cwd=merged_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert branch_ref == expected


def test_resolve_branch_only_ref_returns_none_without_a_merge(tmp_path: Path) -> None:
    """A plain, non-merge commit has no second parent to find."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    (repo / "f.py").write_text("Z = 1\n", encoding="utf-8")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "one commit, no merge"], repo)

    assert resolve_branch_only_ref(repo) is None


def test_scan_at_ref_sees_the_branch_only_tree_not_the_merge_result(merged_repo: Path) -> None:
    """The load-bearing case: scanning the second parent recovers the
    pre-merge tree, where ``caller.py`` (and therefore ``mod``'s only
    caller) is still present -- even though it is gone at ``HEAD``.
    """
    branch_ref = resolve_branch_only_ref(merged_repo)
    assert branch_ref is not None

    def scan_for_orphans(root: Path) -> set[str]:
        return set() if (root / "caller.py").is_file() else {"mod"}

    at_head = scan_for_orphans(merged_repo)
    assert at_head == {"mod"}, "the merge result must show mod as orphaned"

    at_branch = scan_at_ref(branch_ref, merged_repo, scan_for_orphans)
    assert at_branch == set(), "the feature branch's own tree never lost mod's caller"


def test_scan_at_ref_returns_none_for_an_unresolvable_ref(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _run_git(["init", "-q", "-b", "main"], repo)
    assert scan_at_ref("0" * 40, repo, lambda _root: set()) is None


def test_scan_at_ref_leaves_no_worktree_behind(merged_repo: Path) -> None:
    """Cleanup runs even on the success path -- a leaked worktree would
    accumulate across every CI run that hits this code.
    """
    branch_ref = resolve_branch_only_ref(merged_repo)
    assert branch_ref is not None
    scan_at_ref(branch_ref, merged_repo, lambda _root: set())

    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=merged_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("worktree ") == 1, "only the main worktree should remain"
