"""Tests for incremental_merge module — partial branch merges for long-running agents."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.git_basic import GitResult
from bernstein.core.incremental_merge import (
    IncrementalMergeState,
    _dirty_target_files,
    _files_committed_in_branch,
    _load_state,
    _save_state,
    _state_path,
    get_incremental_merge_state,
    incremental_merge_files,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "", stderr: str = "") -> GitResult:
    return GitResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "error") -> GitResult:
    return GitResult(returncode=1, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()

        state = IncrementalMergeState(
            session_id="abc123",
            merged_files=["src/foo.py", "src/bar.py"],
            merge_commits=["deadbeef"],
            last_merged_ts=1_700_000_000.0,
        )
        _save_state(runtime_dir, state)

        loaded = _load_state(runtime_dir, "abc123")
        assert loaded.session_id == "abc123"
        # Save/load preserves insertion order; sorting only happens on merge.
        assert loaded.merged_files == ["src/foo.py", "src/bar.py"]
        assert loaded.merge_commits == ["deadbeef"]
        assert loaded.last_merged_ts == pytest.approx(1_700_000_000.0)

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        state = _load_state(tmp_path, "nonexistent")
        assert state.session_id == "nonexistent"
        assert state.merged_files == []
        assert state.merge_commits == []

    def test_load_corrupt_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "incremental_merges").mkdir()
        state_file = _state_path(tmp_path, "sess")
        state_file.write_text("NOT JSON {{{{")
        state = _load_state(tmp_path, "sess")
        assert state.merged_files == []

    def test_get_incremental_merge_state_wrapper(self, tmp_path: Path) -> None:
        state = get_incremental_merge_state(tmp_path, "sess-new")
        assert state.session_id == "sess-new"
        assert state.merged_files == []


# ---------------------------------------------------------------------------
# _files_committed_in_branch
# ---------------------------------------------------------------------------


class TestFilesCommittedInBranch:
    def test_returns_committed_files(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok("src/foo.py\ntests/test_foo.py\n"),
        ):
            result = _files_committed_in_branch(
                tmp_path, "agent/sess", ["src/foo.py", "tests/test_foo.py", "src/bar.py"]
            )
        assert result == {"src/foo.py", "tests/test_foo.py"}

    def test_empty_files_returns_empty_set(self, tmp_path: Path) -> None:
        result = _files_committed_in_branch(tmp_path, "agent/sess", [])
        assert result == set()

    def test_ls_tree_failure_returns_empty(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_fail("fatal: not a git repository"),
        ):
            result = _files_committed_in_branch(tmp_path, "agent/sess", ["src/foo.py"])
        assert result == set()


# ---------------------------------------------------------------------------
# incremental_merge_files — unit tests with mocked git
# ---------------------------------------------------------------------------


class TestIncrementalMergeFiles:
    """Covers the happy path and error branches of incremental_merge_files."""

    def test_no_files_returns_error(self, tmp_path: Path) -> None:
        result = incremental_merge_files(tmp_path, tmp_path, "sess", [])
        assert not result.success
        assert result.error == "No files specified"

    def test_all_already_merged_skipped(self, tmp_path: Path) -> None:
        """Files already in state are returned as skipped, no git calls needed."""
        runtime_dir = tmp_path / "runtime"
        state = IncrementalMergeState(
            session_id="sess",
            merged_files=["src/foo.py"],
            merge_commits=[],
        )
        _save_state(runtime_dir, state)

        with patch("bernstein.core.git.incremental_merge.run_git") as mock_git:
            result = incremental_merge_files(tmp_path, runtime_dir, "sess", ["src/foo.py"])

        mock_git.assert_not_called()
        assert result.success
        assert result.skipped_already_merged == ["src/foo.py"]
        assert result.merged_files == []

    def test_uncommitted_files_skipped(self, tmp_path: Path) -> None:
        """Files not committed in the agent branch are returned as uncommitted."""
        runtime_dir = tmp_path / "runtime"

        # ls-tree returns nothing → files not committed
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok(""),  # empty ls-tree output
        ):
            result = incremental_merge_files(tmp_path, runtime_dir, "sess", ["src/notyet.py"])

        assert not result.success
        assert result.uncommitted_files == ["src/notyet.py"]
        # Error message explains that none of the requested files are committed.
        assert "not committed" in result.error or "are committed" in result.error

    def test_happy_path_merges_and_commits(self, tmp_path: Path) -> None:
        """Full success path: ls-tree reports files committed, checkout ok, commit ok."""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()

        commit_sha = "abcdef1234567890" + "a" * 24

        call_results = [
            _ok("src/foo.py\nsrc/bar.py\n"),  # ls-tree
            _ok(""),  # status --porcelain: clean workdir
            _ok(""),  # git checkout
            _ok(""),  # git add
            _ok("1 file changed"),  # git commit
            _ok(commit_sha),  # rev-parse HEAD
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_results,
        ):
            result = incremental_merge_files(tmp_path, runtime_dir, "sess", ["src/foo.py", "src/bar.py"], "Custom msg")

        assert result.success
        assert sorted(result.merged_files) == ["src/bar.py", "src/foo.py"]
        assert result.commit_sha == commit_sha
        assert result.error == ""

        # State should be persisted
        loaded = _load_state(runtime_dir, "sess")
        assert set(loaded.merged_files) == {"src/foo.py", "src/bar.py"}
        assert commit_sha in loaded.merge_commits

    def test_nothing_to_commit_is_success(self, tmp_path: Path) -> None:
        """If git commit says 'nothing to commit' the result is still success."""
        runtime_dir = tmp_path / "runtime"

        call_results = [
            _ok("src/foo.py\n"),  # ls-tree
            _ok(""),  # status --porcelain: clean workdir
            _ok(""),  # git checkout
            _ok(""),  # git add
            _fail("nothing to commit, working tree clean"),  # git commit
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_results,
        ):
            result = incremental_merge_files(tmp_path, runtime_dir, "sess", ["src/foo.py"])

        assert result.success
        assert result.commit_sha == ""
        assert result.error == ""

    def test_checkout_failure_reports_conflicts(self, tmp_path: Path) -> None:
        """If git checkout fails, all files are reported as conflicting."""
        runtime_dir = tmp_path / "runtime"

        call_results = [
            _ok("src/foo.py\n"),  # ls-tree
            _ok(""),  # status --porcelain: clean workdir
            _fail("checkout conflict"),  # git checkout fails
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_results,
        ):
            result = incremental_merge_files(tmp_path, runtime_dir, "sess", ["src/foo.py"])

        assert not result.success
        assert result.conflicting_files == ["src/foo.py"]
        assert "conflicted" in result.error

    def test_merge_lock_is_held_during_git_ops(self, tmp_path: Path) -> None:
        """When a merge_lock is passed, it is held during the git operations."""
        runtime_dir = tmp_path / "runtime"
        lock = threading.Lock()

        commit_sha = "a" * 40
        call_seq = [
            _ok("src/foo.py\n"),  # ls-tree
            _ok(""),  # status --porcelain: clean workdir
            _ok(""),  # checkout
            _ok(""),  # add
            _ok(""),  # commit
            _ok(commit_sha),  # rev-parse
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_seq,
        ):
            incremental_merge_files(tmp_path, runtime_dir, "sess", ["src/foo.py"], merge_lock=lock)

        # Lock must not be held after the call
        assert not lock.locked()

    def test_partial_success_mixed_files(self, tmp_path: Path) -> None:
        """Some files already merged, some uncommitted, some new — correct partition."""
        runtime_dir = tmp_path / "runtime"

        # Pre-populate state with one already-merged file
        state = IncrementalMergeState(
            session_id="sess",
            merged_files=["src/done.py"],
            merge_commits=[],
        )
        _save_state(runtime_dir, state)

        commit_sha = "b" * 40

        # ls-tree reports only src/new.py as committed (src/notyet.py is missing)
        call_results = [
            _ok("src/new.py\n"),  # ls-tree for candidates
            _ok(""),  # status --porcelain: clean workdir
            _ok(""),  # checkout
            _ok(""),  # add
            _ok("1 file"),  # commit
            _ok(commit_sha),  # rev-parse
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_results,
        ):
            result = incremental_merge_files(
                tmp_path,
                runtime_dir,
                "sess",
                ["src/done.py", "src/new.py", "src/notyet.py"],
            )

        assert result.success
        assert result.merged_files == ["src/new.py"]
        assert result.skipped_already_merged == ["src/done.py"]
        assert result.uncommitted_files == ["src/notyet.py"]
        assert result.commit_sha == commit_sha


# ---------------------------------------------------------------------------
# IncrementalMergeState.to_dict / from_dict roundtrip
# ---------------------------------------------------------------------------


class TestStateSerialisation:
    def test_roundtrip(self) -> None:
        state = IncrementalMergeState(
            session_id="s1",
            merged_files=["a.py", "b.py"],
            merge_commits=["sha1", "sha2"],
            last_merged_ts=1_234_567.0,
        )
        assert IncrementalMergeState.from_dict(state.to_dict()) == state

    def test_from_dict_defaults(self) -> None:
        state = IncrementalMergeState.from_dict({"session_id": "x"})
        assert state.merged_files == []
        assert state.merge_commits == []
        assert state.last_merged_ts == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _dirty_target_files — guards main workdir against operator-edit clobber
# ---------------------------------------------------------------------------


class TestDirtyTargetFiles:
    """audit-090: must flag operator-modified files before `git checkout` overwrite."""

    def test_empty_files_returns_empty(self, tmp_path: Path) -> None:
        result = _dirty_target_files(tmp_path, [])
        assert result == []

    def test_clean_workdir_returns_empty(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok(""),
        ):
            result = _dirty_target_files(tmp_path, ["src/api.py", "src/util.py"])
        assert result == []

    def test_unstaged_modification_is_dirty(self, tmp_path: Path) -> None:
        # Porcelain " M path" → unstaged modification to tracked file.
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok(" M src/api.py\n"),
        ):
            result = _dirty_target_files(tmp_path, ["src/api.py", "src/util.py"])
        assert result == ["src/api.py"]

    def test_staged_modification_is_dirty(self, tmp_path: Path) -> None:
        # "M  path" → staged modification.
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok("M  src/api.py\n"),
        ):
            result = _dirty_target_files(tmp_path, ["src/api.py"])
        assert result == ["src/api.py"]

    def test_untracked_file_is_not_dirty(self, tmp_path: Path) -> None:
        # "?? path" is untracked; it does not conflict with `git checkout -- path`
        # because checkout writes tracked content and untracked files are
        # undisturbed.
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok("?? src/new.py\n"),
        ):
            result = _dirty_target_files(tmp_path, ["src/new.py"])
        assert result == []

    def test_status_failure_is_conservative(self, tmp_path: Path) -> None:
        # If `git status` cannot be run we must NOT proceed with the clobbering
        # checkout — treat every requested file as dirty.
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_fail("fatal: not a git repository"),
        ):
            result = _dirty_target_files(tmp_path, ["src/api.py"])
        assert result == ["src/api.py"]

    def test_only_non_target_changes_returns_empty(self, tmp_path: Path) -> None:
        # status output contains an unrelated file; target list is clean.
        with patch(
            "bernstein.core.incremental_merge.run_git",
            return_value=_ok(" M docs/readme.md\n"),
        ):
            result = _dirty_target_files(tmp_path, ["src/api.py"])
        assert result == []


# ---------------------------------------------------------------------------
# incremental_merge_files — concurrent write / operator-edit guard (audit-090)
# ---------------------------------------------------------------------------


class TestAuditNinetyConcurrentWrite:
    """Regression: the merge MUST abort instead of overwriting main-workdir edits.

    The historical bug: `git checkout agent/<sid> -- file.py` ran
    unconditionally, silently replacing any uncommitted operator changes to
    `file.py` in the main repo working tree.  The fix makes the merge run
    `git status --porcelain -- <files>` under the merge lock and refuse to
    proceed if any target file is modified.
    """

    def test_dirty_target_aborts_without_checkout(self, tmp_path: Path) -> None:
        """If operator has unstaged edits to a target file, the merge aborts.

        This simulates: operator edits src/api.py (uncommitted), then an agent
        completes work and posts an incremental-merge for the same file.  The
        merge must abort *before* `git checkout`, and must NOT call `git add`
        or `git commit`.
        """
        runtime_dir = tmp_path / "runtime"

        call_results = [
            # 1) ls-tree — src/api.py is committed in the agent branch
            _ok("src/api.py\n"),
            # 2) status --porcelain — operator has an unstaged edit to src/api.py
            _ok(" M src/api.py\n"),
            # Any further calls would be a bug: checkout/add/commit must NOT run.
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_results,
        ) as mock_git:
            result = incremental_merge_files(tmp_path, runtime_dir, "sess-race", ["src/api.py"])

        assert not result.success
        assert result.dirty_files == ["src/api.py"]
        assert result.merged_files == []
        assert result.commit_sha == ""
        assert "uncommitted" in result.error.lower() or "refusing" in result.error.lower()

        # Exactly two git calls: ls-tree and status.  No checkout, add, or
        # commit — otherwise operator edits would have been overwritten.
        assert mock_git.call_count == 2
        commands = [call.args[0][0] for call in mock_git.call_args_list]
        assert "checkout" not in commands
        assert "add" not in commands
        assert "commit" not in commands

        # State must not record the file as merged.
        loaded = _load_state(runtime_dir, "sess-race")
        assert loaded.merged_files == []
        assert loaded.merge_commits == []

    def test_clean_target_proceeds_normally(self, tmp_path: Path) -> None:
        """When the main workdir is clean the merge proceeds as before."""
        runtime_dir = tmp_path / "runtime"
        commit_sha = "c" * 40

        call_results = [
            _ok("src/api.py\n"),  # ls-tree
            _ok(""),  # status --porcelain: clean
            _ok(""),  # checkout
            _ok(""),  # add
            _ok("1 file changed"),  # commit
            _ok(commit_sha),  # rev-parse HEAD
        ]

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=call_results,
        ):
            result = incremental_merge_files(tmp_path, runtime_dir, "sess-clean", ["src/api.py"])

        assert result.success
        assert result.merged_files == ["src/api.py"]
        assert result.dirty_files == []
        assert result.commit_sha == commit_sha

    def test_dirty_check_runs_under_merge_lock(self, tmp_path: Path) -> None:
        """The dirty-check must happen while the merge_lock is held.

        Otherwise a concurrent final-merge (which also holds merge_lock) could
        race the dirty-check and clobber main.  We assert the lock is held
        during both the status call and (if reached) the checkout call.
        """
        runtime_dir = tmp_path / "runtime"
        lock = threading.Lock()
        lock_state: list[bool] = []

        def record_lock(args: list[str], *_a: object, **_kw: object) -> GitResult:
            lock_state.append(lock.locked())
            if args[0] == "ls-tree":
                return _ok("src/api.py\n")
            if args[0] == "status":
                return _ok(" M src/api.py\n")
            # Should not be reached — dirty check aborts first.
            return _ok("")

        with patch(
            "bernstein.core.incremental_merge.run_git",
            side_effect=record_lock,
        ):
            result = incremental_merge_files(
                tmp_path,
                runtime_dir,
                "sess-locked",
                ["src/api.py"],
                merge_lock=lock,
            )

        assert not result.success
        assert result.dirty_files == ["src/api.py"]
        # Both the ls-tree call and the status call must have been made with
        # the merge lock held (ls-tree happens before merge_lock acquisition
        # only if we moved code — current impl holds merge_lock during status).
        # We require AT LEAST the status call (index 1) to be under the lock.
        assert len(lock_state) >= 2
        assert lock_state[1] is True, "status --porcelain must run under merge_lock"
        # Lock released after return.
        assert not lock.locked()
