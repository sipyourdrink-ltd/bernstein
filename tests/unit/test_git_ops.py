"""Tests for bernstein.core.git_ops — centralized git write operations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.git_basic import (
    _classify_change,
    _detect_scope,
    _diff_stat_to_bullets,
    _summarize_from_files,
)
from bernstein.core.git_ops import (
    GitResult,
    PullRequestResult,
    apply_diff,
    bisect_regression,
    branch_delete,
    checkout_discard,
    commit,
    conventional_commit,
    create_branch,
    create_github_pr,
    create_task_branch,
    delete_old_branches,
    diff_cached,
    diff_cached_names,
    diff_cached_stat,
    diff_head,
    enable_pr_auto_merge,
    is_conventional_commit_message,
    is_git_repo,
    merge_branch,
    push_branch,
    push_head_as,
    rev_parse_head,
    revert_commit,
    run_git,
    safe_push,
    stage_all_except,
    stage_files,
    stage_task_files,
    status_porcelain,
    tag,
    unstage_paths,
    worktree_add,
    worktree_list,
    worktree_remove,
)

REPO = Path("/fake/repo")


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mock subprocess.run return value."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestRunGit:
    """Tests for the low-level run_git helper."""

    @patch("bernstein.core.git.git_basic.subprocess.run")
    def test_basic_call(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(stdout="ok\n")
        result = run_git(["status"], REPO)
        assert result.ok
        assert result.stdout == "ok\n"
        mock_run.assert_called_once_with(
            ["git", "status"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            input=None,
        )

    @patch("bernstein.core.git.git_basic.subprocess.run")
    def test_non_zero_exit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=1, stderr="fatal: not a repo")
        result = run_git(["status"], REPO)
        assert not result.ok
        assert result.stderr == "fatal: not a repo"

    @patch("bernstein.core.git.git_basic.subprocess.run")
    def test_check_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=128, stderr="error")
        with pytest.raises(subprocess.CalledProcessError):
            run_git(["status"], REPO, check=True)

    @patch("bernstein.core.git.git_basic.subprocess.run")
    def test_input_data(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run()
        run_git(["apply", "-"], REPO, input_data="diff content")
        mock_run.assert_called_once_with(
            ["git", "apply", "-"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            input="diff content",
        )

    @patch("bernstein.core.git.git_basic.subprocess.run")
    def test_custom_timeout(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run()
        run_git(["push"], REPO, timeout=120)
        assert mock_run.call_args[1]["timeout"] == 120


class TestGitResult:
    """Tests for the GitResult dataclass."""

    def test_ok_property(self) -> None:
        assert GitResult(0, "out", "").ok
        assert not GitResult(1, "", "err").ok

    def test_frozen(self) -> None:
        r = GitResult(0, "a", "b")
        with pytest.raises(AttributeError):
            r.returncode = 1  # type: ignore[misc]


class TestQueries:
    """Tests for read-only git wrappers."""

    @patch("bernstein.core.git_basic.run_git")
    def test_is_git_repo_true(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "true", "")
        assert is_git_repo(REPO)

    @patch("bernstein.core.git_basic.run_git")
    def test_is_git_repo_false(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(128, "", "fatal")
        assert not is_git_repo(REPO)

    @patch("bernstein.core.git_basic.run_git")
    def test_status_porcelain(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, " M file.py\n?? new.py\n", "")
        assert status_porcelain(REPO) == "M file.py\n?? new.py"

    @patch("bernstein.core.git_basic.run_git")
    def test_diff_cached_names(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "a.py\nb.py\n", "")
        assert diff_cached_names(REPO) == ["a.py", "b.py"]

    @patch("bernstein.core.git_basic.run_git")
    def test_diff_cached_names_empty(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        assert diff_cached_names(REPO) == []

    @patch("bernstein.core.git_basic.run_git")
    def test_diff_cached_stat(self, mock: MagicMock) -> None:
        stat_out = "file.py | 10 +++"
        mock.return_value = GitResult(0, stat_out, "")
        assert diff_cached_stat(REPO) == stat_out

    @patch("bernstein.core.git_basic.run_git")
    def test_diff_cached(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "diff --git a/f b/f\n", "")
        assert diff_cached(REPO) == "diff --git a/f b/f\n"

    @patch("bernstein.core.git_basic.run_git")
    def test_diff_head_with_files(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "diff output", "")
        diff_head(REPO, files=["a.py", "b.py"], refs="HEAD~2")
        mock.assert_called_once_with(
            ["diff", "HEAD~2", "--", "a.py", "b.py"],
            REPO,
            timeout=30,
        )

    @patch("bernstein.core.git_basic.run_git")
    def test_rev_parse_head(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "abc123\n", "")
        assert rev_parse_head(REPO) == "abc123"


class TestStaging:
    """Tests for staging operations."""

    @patch("bernstein.core.git_basic.run_git")
    def test_stage_files(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        stage_files(REPO, ["src/a.py", "src/b.py"])
        mock.assert_called_once_with(
            ["add", "--", "src/a.py", "src/b.py"],
            REPO,
        )

    @patch("bernstein.core.git_basic.run_git")
    def test_stage_files_filters_never_stage(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        stage_files(REPO, [".sdd/runtime/state.json", "src/a.py", ".env"])
        mock.assert_called_once_with(["add", "--", "src/a.py"], REPO)

    @patch("bernstein.core.git_basic.run_git")
    def test_stage_files_all_filtered(self, mock: MagicMock) -> None:
        stage_files(REPO, [".sdd/runtime/x", ".env"])
        mock.assert_not_called()

    @patch("bernstein.core.git_basic.run_git")
    def test_stage_task_files(self, mock: MagicMock) -> None:
        # First call is status_porcelain, second is add
        mock.side_effect = [
            GitResult(0, "?? src/test_a.py\n?? unrelated/x.py\n", ""),
            GitResult(0, "", ""),
        ]
        result = stage_task_files(REPO, ["src/a.py"])
        assert "src/a.py" in result
        assert "src/test_a.py" in result
        assert "unrelated/x.py" not in result

    @patch("bernstein.core.git_basic.run_git")
    def test_stage_task_files_empty(self, mock: MagicMock) -> None:
        result = stage_task_files(REPO, [])
        assert result == []
        mock.assert_not_called()

    @patch("bernstein.core.git_basic.run_git")
    def test_unstage_paths(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        unstage_paths(REPO, [".sdd/runtime/", ".sdd/metrics/"])
        mock.assert_called_once_with(
            ["reset", "HEAD", "--", ".sdd/runtime/", ".sdd/metrics/"],
            REPO,
        )

    @patch("bernstein.core.git_basic.run_git")
    def test_stage_all_except(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        stage_all_except(REPO, exclude=[".sdd/metrics/"])
        assert mock.call_count == 2
        mock.assert_any_call(["add", "-A"], REPO)
        # Second call should include the exclude AND never-stage dirs
        reset_call = mock.call_args_list[1]
        args = reset_call[0][0]
        assert "reset" in args
        assert ".sdd/metrics/" in args
        assert ".sdd/runtime/" in args


class TestCommit:
    """Tests for commit operations."""

    @patch("bernstein.core.git_basic.run_git")
    def test_commit(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = commit(REPO, "test message")
        assert result.ok
        mock.assert_called_once_with(["commit", "-m", "test message"], REPO)

    @patch("bernstein.core.git_basic.run_git")
    def test_commit_rejects_invalid_conventional_subject(self, mock: MagicMock) -> None:
        result = commit(REPO, "not conventional", enforce_conventional=True)
        assert not result.ok
        assert "conventional format" in result.stderr
        mock.assert_not_called()

    @patch("bernstein.core.git_basic.run_git")
    def test_commit_accepts_valid_conventional_subject(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = commit(REPO, "fix(core): tighten parsing", enforce_conventional=True)
        assert result.ok
        mock.assert_called_once_with(["commit", "-m", "fix(core): tighten parsing"], REPO)


class TestConventionalCommitValidation:
    """Tests for commit-message validation helper."""

    def test_helper_accepts_optional_scope(self) -> None:
        assert is_conventional_commit_message("feat: add endpoint")
        assert is_conventional_commit_message("fix(router): stop retry loop")

    def test_helper_rejects_invalid_subject(self) -> None:
        assert not is_conventional_commit_message("WIP: temp commit")


class TestConventionalCommit:
    """Tests for conventional commit message generation."""

    @patch("bernstein.core.git.git_basic.commit")
    @patch("bernstein.core.git.git_basic.diff_cached")
    @patch("bernstein.core.git.git_basic.diff_cached_stat")
    @patch("bernstein.core.git.git_basic.diff_cached_names")
    def test_with_task_title(
        self,
        mock_names: MagicMock,
        mock_stat: MagicMock,
        mock_diff: MagicMock,
        mock_commit: MagicMock,
    ) -> None:
        mock_names.return_value = ["src/bernstein/core/server.py"]
        mock_stat.return_value = "src/bernstein/core/server.py | 10 ++"
        mock_diff.return_value = "new file mode 100644\n"
        mock_commit.return_value = GitResult(0, "", "")

        conventional_commit(REPO, task_title="add health endpoint", task_id="T-42")

        msg = mock_commit.call_args[0][1]
        assert msg.startswith("feat(core): add health endpoint")
        assert "Refs: #T-42" in msg
        assert "Co-Authored-By: bernstein[bot]" in msg

    @patch("bernstein.core.git.git_basic.commit")
    @patch("bernstein.core.git.git_basic.diff_cached")
    @patch("bernstein.core.git.git_basic.diff_cached_stat")
    @patch("bernstein.core.git.git_basic.diff_cached_names")
    def test_nothing_staged(
        self,
        mock_names: MagicMock,
        mock_stat: MagicMock,
        mock_diff: MagicMock,
        mock_commit: MagicMock,
    ) -> None:
        mock_names.return_value = []
        result = conventional_commit(REPO)
        assert not result.ok
        mock_commit.assert_not_called()

    @patch("bernstein.core.git.git_basic.commit")
    @patch("bernstein.core.git.git_basic.diff_cached")
    @patch("bernstein.core.git.git_basic.diff_cached_stat")
    @patch("bernstein.core.git.git_basic.diff_cached_names")
    def test_evolve_scope(
        self,
        mock_names: MagicMock,
        mock_stat: MagicMock,
        mock_diff: MagicMock,
        mock_commit: MagicMock,
    ) -> None:
        mock_names.return_value = ["random_file.txt"]
        mock_stat.return_value = ""
        mock_diff.return_value = ""
        mock_commit.return_value = GitResult(0, "", "")

        conventional_commit(REPO, evolve=True)

        msg = mock_commit.call_args[0][1]
        assert "(evolution)" in msg

    @patch("bernstein.core.git.git_basic.commit")
    @patch("bernstein.core.git.git_basic.diff_cached")
    @patch("bernstein.core.git.git_basic.diff_cached_stat")
    @patch("bernstein.core.git.git_basic.diff_cached_names")
    def test_test_files_only(
        self,
        mock_names: MagicMock,
        mock_stat: MagicMock,
        mock_diff: MagicMock,
        mock_commit: MagicMock,
    ) -> None:
        mock_names.return_value = ["tests/test_foo.py", "tests/test_bar.py"]
        mock_stat.return_value = ""
        mock_diff.return_value = ""
        mock_commit.return_value = GitResult(0, "", "")

        conventional_commit(REPO)

        msg = mock_commit.call_args[0][1]
        assert msg.startswith("test(")


class TestSafePush:
    """Tests for safe_push."""

    @patch("bernstein.core.git_basic.run_git")
    @patch("bernstein.core.git.git_basic.fetch")
    def test_no_divergence(self, mock_fetch: MagicMock, mock_run: MagicMock) -> None:
        mock_fetch.return_value = GitResult(0, "", "")
        mock_run.side_effect = [
            GitResult(0, "0\n", ""),  # rev-list --count
            GitResult(0, "", ""),  # push
        ]
        result = safe_push(REPO, "main")
        assert result.ok

    @patch("bernstein.core.git_basic.run_git")
    @patch("bernstein.core.git.git_basic.fetch")
    def test_behind_rebase_success(self, mock_fetch: MagicMock, mock_run: MagicMock) -> None:
        mock_fetch.return_value = GitResult(0, "", "")
        mock_run.side_effect = [
            GitResult(0, "3\n", ""),  # rev-list shows 3 behind
            GitResult(0, "", ""),  # rebase succeeds
            GitResult(0, "", ""),  # push
        ]
        result = safe_push(REPO, "main")
        assert result.ok
        # Should have called rebase
        rebase_call = mock_run.call_args_list[1]
        assert "rebase" in rebase_call[0][0]

    @patch("bernstein.core.git_basic.run_git")
    @patch("bernstein.core.git.git_basic.fetch")
    def test_behind_rebase_fails_merge_fallback(self, mock_fetch: MagicMock, mock_run: MagicMock) -> None:
        mock_fetch.return_value = GitResult(0, "", "")
        mock_run.side_effect = [
            GitResult(0, "2\n", ""),  # behind
            GitResult(1, "", "conflict"),  # rebase fails
            GitResult(0, "", ""),  # rebase --abort
            GitResult(0, "", ""),  # merge fallback
            GitResult(0, "", ""),  # push
        ]
        result = safe_push(REPO, "main")
        assert result.ok


class TestBranching:
    """Tests for branch operations."""

    @patch("bernstein.core.git.git_pr.run_git")
    def test_merge_branch_no_ff(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        merge_branch(REPO, "feature/x", message="Merge feature/x")
        mock.assert_called_once_with(
            ["merge", "--no-ff", "feature/x", "-m", "Merge feature/x"],
            REPO,
            timeout=60,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_merge_branch_ff(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        merge_branch(REPO, "feature/x", no_ff=False)
        mock.assert_called_once_with(
            ["merge", "feature/x"],
            REPO,
            timeout=60,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_branch_delete(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        branch_delete(REPO, "agent/session-1")
        mock.assert_called_once_with(["branch", "-D", "agent/session-1"], REPO, timeout=10)

    @patch("bernstein.core.git_basic.run_git")
    def test_revert_commit_no_commit(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        revert_commit(REPO, "abc123")
        mock.assert_called_once_with(
            ["revert", "--no-commit", "abc123"],
            REPO,
            timeout=30,
        )

    @patch("bernstein.core.git_basic.run_git")
    def test_revert_commit_with_commit(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        revert_commit(REPO, "abc123", no_commit=False)
        mock.assert_called_once_with(
            ["revert", "abc123"],
            REPO,
            timeout=30,
        )

    @patch("bernstein.core.git_basic.run_git")
    def test_checkout_discard(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        checkout_discard(REPO)
        mock.assert_called_once_with(["checkout", "--", "."], REPO)


class TestWorktree:
    """Tests for worktree operations."""

    @patch("bernstein.core.git.git_pr.run_git")
    def test_worktree_add(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        wt_path = Path("/tmp/wt/session-1")
        worktree_add(REPO, wt_path, "agent/session-1")
        mock.assert_called_once_with(
            ["worktree", "add", str(wt_path), "-b", "agent/session-1"],
            REPO,
            timeout=30,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_worktree_remove(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        wt_path = Path("/tmp/wt/session-1")
        worktree_remove(REPO, wt_path)
        mock.assert_called_once_with(
            ["worktree", "remove", "--force", str(wt_path)],
            REPO,
            timeout=30,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_worktree_list(self, mock: MagicMock) -> None:
        porcelain = "worktree /repo\nHEAD abc123\nbranch refs/heads/main\n"
        mock.return_value = GitResult(0, porcelain, "")
        result = worktree_list(REPO)
        assert "worktree /repo" in result

    @patch("bernstein.core.git.git_pr.run_git")
    def test_apply_diff(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        apply_diff(REPO, "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new")
        mock.assert_called_once()
        assert mock.call_args[1]["input_data"].startswith("---")


class TestBisectRegression:
    """Tests for bisect_regression."""

    @patch("bernstein.core.git.git_pr.run_git")
    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_finds_bad_commit(self, mock_sub: MagicMock, mock_git: MagicMock) -> None:
        # bisect start succeeds
        mock_git.side_effect = [
            GitResult(0, "", ""),  # bisect start
            GitResult(0, "", ""),  # bisect reset
        ]
        mock_sub.return_value = _mock_run(stdout="abc1234 is the first bad commit\ncommit abc1234\n")
        result = bisect_regression(REPO, "uv run pytest tests/ -x")
        assert result == "abc1234"

    @patch("bernstein.core.git.git_pr.run_git")
    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_no_bad_commit(self, mock_sub: MagicMock, mock_git: MagicMock) -> None:
        mock_git.side_effect = [
            GitResult(0, "", ""),  # bisect start
            GitResult(0, "", ""),  # bisect reset
        ]
        mock_sub.return_value = _mock_run(stdout="bisect complete\n")
        result = bisect_regression(REPO, "pytest")
        assert result is None

    @patch("bernstein.core.git.git_pr.run_git")
    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_timeout_returns_none(self, mock_sub: MagicMock, mock_git: MagicMock) -> None:
        mock_git.side_effect = [
            GitResult(0, "", ""),  # bisect start
            GitResult(0, "", ""),  # bisect reset
        ]
        mock_sub.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=600)
        result = bisect_regression(REPO, "pytest")
        assert result is None


class TestTag:
    """Tests for git tag."""

    @patch("bernstein.core.git_basic.run_git")
    def test_lightweight_tag(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        tag(REPO, "v1.0.0")
        mock.assert_called_once_with(["tag", "v1.0.0"], REPO)

    @patch("bernstein.core.git_basic.run_git")
    def test_annotated_tag(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        tag(REPO, "v1.0.0", message="Release 1.0.0")
        mock.assert_called_once_with(
            ["tag", "-a", "v1.0.0", "-m", "Release 1.0.0"],
            REPO,
        )


class TestConventionalCommitHelpers:
    """Tests for internal helpers used by conventional_commit."""

    def test_classify_change_all_tests(self) -> None:
        assert _classify_change(["tests/test_a.py", "tests/test_b.py"], "") == "test"

    def test_classify_change_all_docs(self) -> None:
        assert _classify_change(["README.md", "docs/guide.rst"], "") == "docs"

    def test_classify_change_all_config(self) -> None:
        assert _classify_change(["pyproject.toml", ".gitignore"], "") == "chore"

    def test_classify_change_new_files(self) -> None:
        diff = "new file mode 100644\nnew file mode 100644\n"
        assert _classify_change(["a.py", "b.py"], diff) == "feat"

    def test_classify_change_modifications(self) -> None:
        assert _classify_change(["src/a.py"], "") == "refactor"

    def test_detect_scope_core(self) -> None:
        assert _detect_scope(["src/bernstein/core/server.py"]) == "core"

    def test_detect_scope_evolution(self) -> None:
        assert _detect_scope(["src/bernstein/evolution/loop.py"]) == "evolution"

    def test_detect_scope_tests(self) -> None:
        assert _detect_scope(["tests/unit/test_foo.py"]) == "tests"

    def test_detect_scope_mixed(self) -> None:
        files = [
            "src/bernstein/core/a.py",
            "src/bernstein/core/b.py",
            "src/bernstein/cli/c.py",
        ]
        assert _detect_scope(files) == "core"

    def test_detect_scope_empty(self) -> None:
        assert _detect_scope([]) == "unknown"

    def test_summarize_from_files(self) -> None:
        result = _summarize_from_files(["src/server.py", "src/client.py"])
        assert "server" in result
        assert "client" in result

    def test_summarize_from_files_truncated(self) -> None:
        files = [f"src/mod{i}.py" for i in range(10)]
        result = _summarize_from_files(files)
        assert "(+7 more)" in result

    def test_diff_stat_to_bullets(self) -> None:
        stat = " src/a.py | 10 +++++\n src/b.py |  5 +--\n 2 files changed, 12 insertions(+), 3 deletions(-)\n"
        bullets = _diff_stat_to_bullets(stat)
        assert len(bullets) == 3
        assert "src/a.py" in bullets[0]
        assert "2 files changed" in bullets[2]

    def test_diff_stat_to_bullets_empty(self) -> None:
        assert _diff_stat_to_bullets("") == []


# ---------------------------------------------------------------------------
# Pull request helpers
# ---------------------------------------------------------------------------


class TestPullRequestResult:
    """Tests for the PullRequestResult dataclass."""

    def test_success(self) -> None:
        r = PullRequestResult(success=True, pr_url="https://github.com/a/b/pull/1")
        assert r.success
        assert r.pr_url == "https://github.com/a/b/pull/1"

    def test_failure(self) -> None:
        r = PullRequestResult(success=False, error="gh: not found")
        assert not r.success
        assert r.error == "gh: not found"
        assert r.pr_url == ""

    def test_frozen(self) -> None:
        r = PullRequestResult(success=True)
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]


class TestCreateBranch:
    """Tests for create_branch — creates a branch from a base without checkout."""

    @patch("bernstein.core.git.git_pr.run_git")
    def test_creates_from_main(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = create_branch(REPO, "bernstein/task-abc123")
        assert result.ok
        mock.assert_called_once_with(
            ["branch", "bernstein/task-abc123", "main"],
            REPO,
            timeout=10,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_creates_from_custom_base(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = create_branch(REPO, "evolve/iteration-5", base="develop")
        assert result.ok
        mock.assert_called_once_with(
            ["branch", "evolve/iteration-5", "develop"],
            REPO,
            timeout=10,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_returns_failure_on_exists(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(1, "", "fatal: a branch named 'x' already exists")
        result = create_branch(REPO, "x")
        assert not result.ok


class TestDeleteOldBranches:
    """Tests for delete_old_branches — auto-cleanup of stale branches."""

    @patch("bernstein.core.git.git_pr.time.time", return_value=1_000_000.0)
    @patch("bernstein.core.git.git_pr.run_git")
    def test_deletes_old_branches(self, mock_git: MagicMock, _mock_time: MagicMock) -> None:
        # Branch list: one old (epoch 900000 = ~28h ago), one recent (epoch 999000 = ~17min ago)
        branch_list = "bernstein/task-old 900000\nbernstein/task-new 999000\n"
        mock_git.side_effect = [
            GitResult(0, branch_list, ""),  # branch --list
            GitResult(0, "", ""),  # branch -D (old)
        ]
        deleted = delete_old_branches(REPO, older_than_hours=24)
        assert deleted == ["bernstein/task-old"]
        # Should not try to delete the recent branch
        assert mock_git.call_count == 2

    @patch("bernstein.core.git.git_pr.time.time", return_value=1_000_000.0)
    @patch("bernstein.core.git.git_pr.run_git")
    def test_no_old_branches(self, mock_git: MagicMock, _mock_time: MagicMock) -> None:
        branch_list = "bernstein/task-new 999000\n"
        mock_git.side_effect = [
            GitResult(0, branch_list, ""),  # branch --list
        ]
        deleted = delete_old_branches(REPO, older_than_hours=24)
        assert deleted == []

    @patch("bernstein.core.git.git_pr.time.time", return_value=1_000_000.0)
    @patch("bernstein.core.git.git_pr.run_git")
    def test_also_deletes_remote(self, mock_git: MagicMock, _mock_time: MagicMock) -> None:
        branch_list = "bernstein/task-stale 800000\n"
        mock_git.side_effect = [
            GitResult(0, branch_list, ""),  # branch --list
            GitResult(0, "", ""),  # branch -D
            GitResult(0, "", ""),  # push --delete
        ]
        deleted = delete_old_branches(REPO, older_than_hours=24, remote="origin")
        assert deleted == ["bernstein/task-stale"]
        # Verify remote delete was called
        push_call = mock_git.call_args_list[2]
        assert push_call[0][0] == ["push", "origin", "--delete", "bernstein/task-stale"]

    @patch("bernstein.core.git.git_pr.run_git")
    def test_empty_branch_list(self, mock_git: MagicMock) -> None:
        mock_git.return_value = GitResult(0, "", "")
        deleted = delete_old_branches(REPO)
        assert deleted == []


class TestCreateTaskBranch:
    """Tests for create_task_branch."""

    @patch("bernstein.core.git.git_pr.run_git")
    def test_creates_branch(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = create_task_branch(REPO, "bernstein/task-abc123")
        assert result.ok
        mock.assert_called_once_with(
            ["checkout", "-b", "bernstein/task-abc123"],
            REPO,
            timeout=10,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_returns_failure_on_branch_exists(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(1, "", "fatal: branch already exists")
        result = create_task_branch(REPO, "bernstein/task-abc123")
        assert not result.ok


class TestPushBranch:
    """Tests for push_branch."""

    @patch("bernstein.core.git.git_pr.run_git")
    def test_pushes_with_upstream(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = push_branch(REPO, "bernstein/task-abc123")
        assert result.ok
        mock.assert_called_once_with(
            ["push", "--set-upstream", "origin", "bernstein/task-abc123"],
            REPO,
            timeout=60,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_custom_remote(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        push_branch(REPO, "feature/x", remote="upstream")
        mock.assert_called_once_with(
            ["push", "--set-upstream", "upstream", "feature/x"],
            REPO,
            timeout=60,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_returns_failure_on_push_error(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(1, "", "remote: Permission denied")
        result = push_branch(REPO, "feature/x")
        assert not result.ok


class TestPushHeadAs:
    """Tests for push_head_as — pushes HEAD as a named remote branch."""

    @patch("bernstein.core.git.git_pr.run_git")
    def test_pushes_head_via_refspec(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = push_head_as(REPO, "bernstein/task-abc123")
        assert result.ok
        mock.assert_called_once_with(
            ["push", "--set-upstream", "origin", "HEAD:refs/heads/bernstein/task-abc123"],
            REPO,
            timeout=60,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_custom_remote(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        push_head_as(REPO, "bernstein/task-xyz", remote="upstream")
        mock.assert_called_once_with(
            ["push", "--set-upstream", "upstream", "HEAD:refs/heads/bernstein/task-xyz"],
            REPO,
            timeout=60,
        )

    @patch("bernstein.core.git.git_pr.run_git")
    def test_returns_failure_on_push_error(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(1, "", "remote: Permission denied")
        result = push_head_as(REPO, "bernstein/task-fail")
        assert not result.ok


class TestCreateGithubPr:
    """Tests for create_github_pr — delegates to the gh CLI."""

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=0, stdout="https://github.com/owner/repo/pull/42\n")
        result = create_github_pr(
            REPO,
            title="feat: add PR support",
            body="Adds PR creation",
            head="bernstein/task-abc123",
        )
        assert result.success
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "pr" in cmd
        assert "create" in cmd
        assert "--title" in cmd
        assert "feat: add PR support" in cmd
        assert "--head" in cmd
        assert "bernstein/task-abc123" in cmd

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_with_labels(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=0, stdout="https://github.com/owner/repo/pull/5\n")
        create_github_pr(
            REPO,
            title="task",
            body="body",
            head="bernstein/task-x",
            labels=["bernstein", "auto-generated"],
        )
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd or "--label" in cmd
        label_flag = "--add-label" if "--add-label" in cmd else "--label"
        label_idx = cmd.index(label_flag)
        assert "bernstein" in cmd[label_idx + 1]
        assert "auto-generated" in cmd[label_idx + 1]

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_failure_returns_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=1, stderr="gh: authentication required")
        result = create_github_pr(
            REPO,
            title="task",
            body="body",
            head="bernstein/task-x",
        )
        assert not result.success
        assert "authentication required" in result.error

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_gh_not_installed(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("gh not found")
        result = create_github_pr(
            REPO,
            title="task",
            body="body",
            head="bernstein/task-x",
        )
        assert not result.success
        assert result.error != ""

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_custom_base_branch(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=0, stdout="https://github.com/x/y/pull/1\n")
        create_github_pr(REPO, title="t", body="b", head="feature/x", base="develop")
        cmd = mock_run.call_args[0][0]
        assert "--base" in cmd
        base_idx = cmd.index("--base")
        assert cmd[base_idx + 1] == "develop"


class TestEnablePrAutoMerge:
    """Tests for enable_pr_auto_merge."""

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_enables_auto_merge(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=0)
        result = enable_pr_auto_merge(REPO, "https://github.com/owner/repo/pull/42")
        assert result.ok
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "pr" in cmd
        assert "merge" in cmd
        assert "--auto" in cmd

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=1, stderr="error")
        result = enable_pr_auto_merge(REPO, "42")
        assert not result.ok

    @patch("bernstein.core.git.git_pr.subprocess.run")
    def test_timeout_returns_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        result = enable_pr_auto_merge(REPO, "42")
        assert not result.ok
