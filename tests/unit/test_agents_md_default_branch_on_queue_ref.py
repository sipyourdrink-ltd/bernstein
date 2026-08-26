"""The rendered default branch must not become a merge-queue ref.

GitHub checks a queued group out as a *real branch* named
``gh-readonly-queue/<base>/pr-<n>-<base_sha>``. The detached-HEAD guard in
``_git_default_branch`` therefore does not fire, and without the queue-ref
case the generator renders that ephemeral name into AGENTS.md's "Git
workflow" section. The mirror-drift guard then fails on every queued entry,
so the queue rejects each PR in turn for a defect none of them contain.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.knowledge.agents_md_generator import (
    _base_branch_of_merge_queue_ref,
    _git_default_branch,
)


def _repo_on_branch(root: Path, branch: str) -> Path:
    """A git repo whose only branch is ``branch`` and which has no origin.

    No ``main``/``master`` ref and no ``origin/HEAD`` means resolution falls
    through to the merge-queue-ref probe, which is the step under test. Any
    ordinary branch name here is deliberately *not* a valid answer (issue
    #4578): the default branch is a repository property, so an origin-less
    single-branch checkout has none to report.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def test_a_queue_ref_resolves_to_the_branch_it_was_queued_against(tmp_path: Path) -> None:
    """The regression: this returned the whole ``gh-readonly-queue/...`` name."""
    root = _repo_on_branch(
        tmp_path / "queued",
        "gh-readonly-queue/main/pr-3814-7c4a39fd277650c166efa1df97e4082cbdc9dee2",
    )

    assert _git_default_branch(root) == "main"


def test_a_queue_ref_against_a_slashed_base_keeps_the_whole_base(tmp_path: Path) -> None:
    """The base branch may contain slashes; the ``pr-`` segment anchors the split."""
    root = _repo_on_branch(
        tmp_path / "release",
        "gh-readonly-queue/release/v3/pr-12-0123456789abcdef0123456789abcdef01234567",
    )

    assert _git_default_branch(root) == "release/v3"


def test_an_ordinary_branch_without_repo_default_is_unresolved(tmp_path: Path) -> None:
    """An ordinary (non queue-ref) branch name must NOT be used as the default.

    Regression for #4578: this used to return ``trunk``, which made
    ``bernstein agents-md sync`` write the checked-out branch into the
    committed context files whenever the conventional ``main``/``master``
    refs were absent (e.g. a single-branch CI clone). The default branch is
    a repository property; a checkout on a feature branch has no standing to
    answer for it, so ``None`` (unresolved) is the only correct outcome.
    """
    root = _repo_on_branch(tmp_path / "ordinary", "trunk")

    assert _git_default_branch(root) is None


@pytest.mark.parametrize(
    "branch",
    [
        "gh-readonly-queue/main",
        "gh-readonly-queue/main/pr-3814",
        "gh-readonly-queue/main/pr-abc-7c4a39fd277650c166efa1df97e4082cbdc9dee2",
        "feature/gh-readonly-queue/main/pr-1-7c4a39fd2",
        "main",
    ],
)
def test_a_branch_that_only_resembles_a_queue_ref_is_not_rewritten(branch: str) -> None:
    """A partial match must not silently truncate a real branch name."""
    assert _base_branch_of_merge_queue_ref(branch) is None
