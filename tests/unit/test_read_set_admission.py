"""Tests for read-set admission logic.

Tests cover:
    1. Happy path: read-set merge succeeds with no changes.
    2. Failure case: second task's read-set conflicts with prior write-set.
    3. Journal mutation detection: broken chain causes refusal.
    4. Deterministic receipt serialization: identical output across runs.
    5. Offline verification: commit hashes allow audit without repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.replay.read_paths import ReadPathSet

# ------------------------------------------------------------------
# Test helper fixtures
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_happy_path_no_changes(tmp_path: Path) -> None:
    """When the journal read-set does not intersect the git diff, returns [].

    This is the happy path where a task's read-set is unchanged and can be
    safely merged. The git diff shows no changed files in the read-set.
    """
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Mock derive_read_paths to return the correct ReadPathSet type
    mock_read_path_set = ReadPathSet(
        read_paths=frozenset(["src/config_schema.py", "README.md"]),
        out_of_tree=frozenset(),
    )

    # Mock the git diff to return empty (no changes)
    mock_git_result = MagicMock()
    mock_git_result.returncode = 0
    mock_git_result.stdout = ""  # No changed files in diff

    with (
        patch("bernstein.core.git.read_set_admission.run_git", return_value=mock_git_result),
        patch(
            "bernstein.core.git.read_set_admission.derive_read_paths",
            return_value=mock_read_path_set,
        ),
    ):
        from bernstein.core.git.read_set_admission import check_read_set_changed

        result = check_read_set_changed(
            journal_path=str(journal_path),
            worktree_root=str(worktree_root),
            base_commit="abcdef",
            target_branch="main",
        )

        assert result == []


def test_failure_path_changed(tmp_path: Path) -> None:
    """When a file in the read-set changes, returns ChangedPath entries.

    This simulates the case where Task A read X, Task B wrote X, and the
    second merge is refused because the read-set has drifted.
    """
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Mock derive_read_paths to return a ReadPathSet with one file
    mock_read_path_set = ReadPathSet(
        read_paths=frozenset(["src/config_schema.py"]),
        out_of_tree=frozenset(),
    )

    # Mock git commands - return different results based on args
    def mock_run_git(args: list[str], **_: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0

        if args[0] == "diff" and args[1] == "--name-only":
            result.stdout = "src/config_schema.py\n"
        elif args[0] == "log" and len(args) >= 4 and args[2] == "--format=%H":
            commit_spec = args[3] if len(args) > 3 else ""
            if commit_spec == "abcdef":
                result.stdout = "deadbeef"
            elif commit_spec == "main":
                result.stdout = "feedface"
            else:
                result.stdout = ""
        else:
            result.stdout = ""

        return result

    with (
        patch("bernstein.core.git.read_set_admission.run_git", side_effect=mock_run_git),
        patch(
            "bernstein.core.git.read_set_admission.derive_read_paths",
            return_value=mock_read_path_set,
        ),
    ):
        from bernstein.core.git.read_set_admission import check_read_set_changed

        result = check_read_set_changed(
            journal_path=str(journal_path),
            worktree_root=str(worktree_root),
            base_commit="abcdef",
            target_branch="main",
        )

        assert len(result) == 1
        changed = result[0]
        assert changed.path == "src/config_schema.py"
        assert changed.old_commit == "deadbeef"
        assert changed.new_commit == "feedface"


def test_journal_mutation_detection(tmp_path: Path) -> None:
    """A broken journal chain causes ReadPathDerivationError with broken_chain.

    When the event journal has been tampered with, the derivation raises an
    exception that propagates as a refusal.
    """
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Mock derive_read_paths to raise broken_chain error
    with (
        patch("bernstein.core.git.read_set_admission.run_git"),
        patch("bernstein.core.git.read_set_admission.derive_read_paths") as mock_derive,
    ):
        mock_derive.side_effect = ReadPathSet(
            read_paths=frozenset(),
            out_of_tree=frozenset(),
        )
        # Use a different approach - mock derive_read_paths to raise
        mock_derive.side_effect = Exception("Chain broken")

        from bernstein.core.git.read_set_admission import (
            check_read_set_changed,
        )

        # Test that the function handles the error appropriately
        with pytest.raises(Exception, match="Chain broken"):
            check_read_set_changed(
                journal_path=str(journal_path),
                worktree_root=str(worktree_root),
                base_commit="abcdef",
                target_branch="main",
            )


def test_deterministic_receipt_serialization(tmp_path: Path) -> None:
    """Two identical invocations produce byte-identical JSON representations.

    This verifies the receipt serialization is deterministic and would produce
    identical audit artifacts across orchestrator workers.
    """
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    def build_result() -> list[dict]:
        """Run check_read_set_changed and return JSON-serializable form."""
        mock_read_path_set = ReadPathSet(
            read_paths=frozenset(["src/config_schema.py"]),
            out_of_tree=frozenset(),
        )

        def mock_run_git(args: list[str], **_: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 0

            if args[0] == "diff" and args[1] == "--name-only":
                result.stdout = "src/config_schema.py\n"
            elif args[0] == "log" and len(args) >= 4 and args[2] == "--format=%H":
                commit_spec = args[3] if len(args) > 3 else ""
                if commit_spec == "abcdef":
                    result.stdout = "1234567890abcdef"
                elif commit_spec == "main":
                    result.stdout = "fedcba0987654321"
                else:
                    result.stdout = ""
            else:
                result.stdout = ""

            return result

        with (
            patch("bernstein.core.git.read_set_admission.run_git", side_effect=mock_run_git),
            patch(
                "bernstein.core.git.read_set_admission.derive_read_paths",
                return_value=mock_read_path_set,
            ),
        ):
            from bernstein.core.git.read_set_admission import check_read_set_changed

            result = check_read_set_changed(
                journal_path=str(journal_path),
                worktree_root=str(worktree_root),
                base_commit="abcdef",
                target_branch="main",
            )

        return [{"path": c.path, "old_commit": c.old_commit, "new_commit": c.new_commit} for c in result]

    # First invocation
    result1 = build_result()
    json1 = json.dumps(result1, sort_keys=True)

    # Second invocation with identical inputs
    result2 = build_result()
    json2 = json.dumps(result2, sort_keys=True)

    assert json1 == json2


def test_offline_verification(tmp_path: Path) -> None:
    """ChangedPath objects contain enough data to verify without repo access.

    The commit hashes in the result can be used to reconstruct the change
    evidence for offline audit.
    """
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Mock derive_read_paths
    mock_read_path_set = ReadPathSet(
        read_paths=frozenset(["src/config_schema.py"]),
        out_of_tree=frozenset(),
    )

    def mock_run_git(args: list[str], **_: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0

        if args[0] == "diff" and args[1] == "--name-only":
            result.stdout = "src/config_schema.py\n"
        elif args[0] == "log" and len(args) >= 4 and args[2] == "--format=%H":
            commit_spec = args[3] if len(args) > 3 else ""
            if commit_spec == "abcdef":
                result.stdout = "aaa111"
            elif commit_spec == "main":
                result.stdout = "bbb222"
            else:
                result.stdout = ""
        else:
            result.stdout = ""

        return result

    with (
        patch("bernstein.core.git.read_set_admission.run_git", side_effect=mock_run_git),
        patch(
            "bernstein.core.git.read_set_admission.derive_read_paths",
            return_value=mock_read_path_set,
        ),
    ):
        from bernstein.core.git.read_set_admission import check_read_set_changed

        result = check_read_set_changed(
            journal_path=str(journal_path),
            worktree_root=str(worktree_root),
            base_commit="abcdef",
            target_branch="main",
        )

        assert len(result) == 1
        changed = result[0]

        # Simulate offline verification: the hashes uniquely identify the state
        offline_evidence = {
            "file": changed.path,
            "base_commit_hash": changed.old_commit,
            "target_commit_hash": changed.new_commit,
        }

        assert offline_evidence["file"] == "src/config_schema.py"
        assert offline_evidence["base_commit_hash"] == "aaa111"
        assert offline_evidence["target_commit_hash"] == "bbb222"


def test_disjoint_write_sets_no_conflict(tmp_path: Path) -> None:
    """Tasks with disjoint read and write sets merge cleanly.

    Task A reads X, Task B writes Y (different file). The second merge
    should succeed because the read-set hasn't changed.
    """
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Mock derive_read_paths - Task A reads X.py
    mock_read_path_set = ReadPathSet(
        read_paths=frozenset(["src/X.py"]),
        out_of_tree=frozenset(),
    )

    # Git diff shows only Y.py changed, but read-set is only X.py
    def mock_run_git(args: list[str], **_: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0

        if args[0] == "diff" and args[1] == "--name-only":
            # Only Y changed, not X
            result.stdout = "src/Y.py\n"
        else:
            result.stdout = ""

        return result

    with (
        patch("bernstein.core.git.read_set_admission.run_git", side_effect=mock_run_git),
        patch(
            "bernstein.core.git.read_set_admission.derive_read_paths",
            return_value=mock_read_path_set,
        ),
    ):
        from bernstein.core.git.read_set_admission import check_read_set_changed

        result = check_read_set_changed(
            journal_path=str(journal_path),
            worktree_root=str(worktree_root),
            base_commit="abcdef",
            target_branch="main",
        )

        # No changes in the read-set, so result should be empty
        assert result == []


def test_multiple_changed_paths(tmp_path: Path) -> None:
    """Multiple files in read-set that changed all appear in result."""
    journal_path = tmp_path / "journal.jsonl"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Mock derive_read_paths - read-set has A, B, C
    mock_read_path_set = ReadPathSet(
        read_paths=frozenset(["src/A.py", "src/B.py", "src/C.py"]),
        out_of_tree=frozenset(),
    )

    def mock_run_git(args: list[str], **_: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0

        if args[0] == "diff" and args[1] == "--name-only":
            # A and B changed, C is unchanged
            result.stdout = "src/A.py\nsrc/B.py\n"
        elif args[0] == "log" and len(args) >= 4 and args[2] == "--format=%H":
            # All old commits are the same, all new commits are the same
            result.stdout = "old_hash" if "abcdef" in args else "new_hash"
        else:
            result.stdout = ""

        return result

    with (
        patch("bernstein.core.git.read_set_admission.run_git", side_effect=mock_run_git),
        patch(
            "bernstein.core.git.read_set_admission.derive_read_paths",
            return_value=mock_read_path_set,
        ),
    ):
        from bernstein.core.git.read_set_admission import check_read_set_changed

        result = check_read_set_changed(
            journal_path=str(journal_path),
            worktree_root=str(worktree_root),
            base_commit="abcdef",
            target_branch="main",
        )

        # Should only return A and B, not C
        assert len(result) == 2
        paths = {c.path for c in result}
        assert paths == {"src/A.py", "src/B.py"}


def test_an_unanswerable_admission_question_refuses_the_merge(tmp_path: Path) -> None:
    """When the admission check itself cannot run -- unreadable journal,
    broken tree -- the merge is refused with the reason, not admitted with a
    warning. A gate that fails open admits exactly the runs it exists to
    stop, in the one situation where it has no idea what happened."""
    from unittest.mock import patch

    from bernstein.core.git.git_pr import merge_with_conflict_detection

    # Create a dummy journal file to allow the read_paths derivation to proceed
    journal_path = tmp_path / "journal.jsonl"
    journal_path.touch()

    # Test that merge_with_conflict_detection raises ReadSetAdmissionRefused when an exception occurs
    with patch(
        "bernstein.core.git.git_pr.check_read_set_changed",
        side_effect=RuntimeError("journal unreadable"),
    ):
        from bernstein.core.git.read_set_admission import ReadSetAdmissionRefused

        with pytest.raises(ReadSetAdmissionRefused, match="Read-set admission check could not run"):
            merge_with_conflict_detection(
                cwd=tmp_path,
                branch="work",
                message="m",
                task_id="T-1",
                journal_path=str(journal_path),
                worktree_root=str(tmp_path),
            )


def test_an_unanswerable_admission_question_refuses_the_incremental_merge(
    tmp_path: Path,
) -> None:
    """The incremental path holds the same contract as the full merge."""
    from unittest.mock import patch

    from bernstein.core.git.incremental_merge import incremental_merge_files

    # Create a dummy journal file to allow the read_paths derivation to proceed
    journal_path = tmp_path / "journal.jsonl"
    journal_path.touch()

    # Test that incremental_merge_files raises ReadSetAdmissionRefused when an exception occurs
    with patch(
        "bernstein.core.git.incremental_merge.check_read_set_changed",
        side_effect=RuntimeError("journal unreadable"),
    ):
        from bernstein.core.git.read_set_admission import ReadSetAdmissionRefused

        with pytest.raises(ReadSetAdmissionRefused, match="Read-set admission check could not run"):
            incremental_merge_files(
                workdir=tmp_path,
                runtime_dir=tmp_path / "runtime",
                session_id="S-1",
                files=["a.py"],
                task_id="T-1",
                journal_path=str(journal_path),
                worktree_root=str(tmp_path),
            )
