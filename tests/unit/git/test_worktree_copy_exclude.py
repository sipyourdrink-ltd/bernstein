"""Files copied into an agent worktree must not be committable from it.

`copy_files` gives each worktree its own copy of untracked per-checkout inputs.
Names in `_MERGE_DENY_EXACT` (`.env` and friends) were already excluded by the
worktree-local excludes file, but any other name -- `secrets.env`,
`config/local.toml` -- was stageable: an agent running `git add -A` committed it
to its branch, and the merge back into the parent repository failed with an
untracked-overwrite error naming a file the agent was never asked to touch
(#5966).

The exclusion is scoped to the one worktree through the same per-worktree
`core.excludesFile` that #3017 uses, never the shared `info/exclude`, so the
operator's own checkout and every other worktree of the clone are unaffected.

Real repositories and real linked worktrees, created through
`WorktreeManager.create` as in production: the whole question is what `git`
does with these paths, and a mocked `git` cannot answer it.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.git.git_pr import _MERGE_DENY_EXACT
from bernstein.core.git.worktree import (
    WorktreeManager,
    WorktreeSetupConfig,
    _copy_files_exclude_entries,
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _ignored(cwd: Path, rel: str) -> bool:
    """True when git in *cwd* would ignore *rel*."""
    return _git(cwd, "check-ignore", "-q", "--no-index", rel, check=False).returncode == 0


def _staged(cwd: Path) -> list[str]:
    _git(cwd, "add", "-A")
    return _git(cwd, "diff", "--cached", "--name-only").stdout.split()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Bernstein Tests")
    _git(root, "config", "user.email", "tests@example.com")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "tracked.env").write_text("TRACKED=1\n", encoding="utf-8")
    _git(root, "add", "app.py", "config/tracked.env")
    _git(root, "commit", "-m", "base")
    # The per-checkout inputs: untracked in the parent, which is exactly why a
    # copy of one arriving through a merge cannot be written. Neither name is
    # in the merge guard's deny list, so neither was excluded before.
    (root / "secrets.env").write_text("TOKEN=parent\n", encoding="utf-8")
    (root / "config" / "local.toml").write_text("k = 1\n", encoding="utf-8")
    return root


def _create(repo_root: Path, *copy: str, session: str = "agent") -> Path:
    manager = WorktreeManager(repo_root=repo_root, setup_config=WorktreeSetupConfig(copy_files=copy))
    return manager.create(session)


def test_names_are_outside_the_existing_deny_list() -> None:
    """Guards the premise: `.env` was already excluded, these were not."""
    assert "secrets.env" not in _MERGE_DENY_EXACT
    assert "config/local.toml" not in _MERGE_DENY_EXACT


def test_copied_inputs_are_not_staged_by_add_all(repo: Path) -> None:
    """The agent's `git add -A` leaves the copied inputs out of the index."""
    wt = _create(repo, "secrets.env", "config/local.toml")
    assert (wt / "secrets.env").is_file(), "the copy itself still happens"
    (wt / "feature.py").write_text("y = 2\n", encoding="utf-8")

    staged = _staged(wt)

    assert "feature.py" in staged
    assert "secrets.env" not in staged
    assert "config/local.toml" not in staged


def test_operator_checkout_is_unaffected(repo: Path) -> None:
    """Nothing reaches the shared git dir: the parent still sees its own files."""
    _create(repo, "secrets.env", "config/local.toml")

    assert not _ignored(repo, "secrets.env")
    assert not _ignored(repo, "config/local.toml")
    info_exclude = repo / ".git" / "info" / "exclude"
    if info_exclude.exists():
        assert "secrets.env" not in info_exclude.read_text(encoding="utf-8")


def test_other_worktrees_are_unaffected(repo: Path) -> None:
    """A second worktree created without `copy_files` can still stage the name."""
    _create(repo, "secrets.env", session="with-copy")
    other = WorktreeManager(repo_root=repo, setup_config=None).create("without-copy")
    (other / "secrets.env").write_text("TOKEN=other\n", encoding="utf-8")

    assert "secrets.env" in _staged(other)


def test_entries_are_anchored(repo: Path) -> None:
    """`/secrets.env` must not hide a same-named file the project keeps elsewhere."""
    wt = _create(repo, "secrets.env")
    (wt / "config" / "secrets.env").write_text("nested\n", encoding="utf-8")

    assert "config/secrets.env" in _staged(wt)


def test_every_configured_name_is_excluded_not_only_fresh_copies(repo: Path) -> None:
    """A name whose source is missing at create time is still excluded.

    `_copy_files` skips it, but the agent (or a setup command) can create the
    file later, and it is just as stageable then.
    """
    wt = _create(repo, "later.env")
    (wt / "later.env").write_text("x\n", encoding="utf-8")

    assert "later.env" not in _staged(wt)


class TestEntryBuilder:
    def test_metacharacters_are_literal(self, tmp_path: Path) -> None:
        entries = _copy_files_exclude_entries(tmp_path, ["a[1].env", "b*.env", "#c.env", "!d.env"])
        assert entries == ("/a\\[1].env", "/b\\*.env", "/#c.env", "/!d.env")

    def test_bracketed_name_matches_only_itself(self, repo: Path) -> None:
        (repo / "a[1].env").write_text("x\n", encoding="utf-8")
        wt = _create(repo, "a[1].env")
        (wt / "a1.env").write_text("deliverable\n", encoding="utf-8")

        staged = _staged(wt)

        assert "a[1].env" not in staged
        assert "a1.env" in staged

    def test_directories_and_escapes_are_skipped(self, repo: Path) -> None:
        """An anchored `/config` would hide every new file the agent writes there."""
        entries = _copy_files_exclude_entries(repo, ["config", "../outside.env", "", "secrets.env"])
        assert entries == ("/secrets.env",)

    def test_paths_are_normalised_to_posix(self, repo: Path) -> None:
        assert _copy_files_exclude_entries(repo, ["./config/../config/local.toml"]) == ("/config/local.toml",)
