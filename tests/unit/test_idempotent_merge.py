"""Tests for idempotent merge with conflict pre-check."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bernstein.core.idempotent_merge import (
    MergeAttempt,
    MergeCheckResult,
    build_merge_command,
    create_merge_attempt,
    dry_run_merge,
    format_merge_check,
    should_attempt_merge,
)

# ---------------------------------------------------------------------------
# MergeCheckResult
# ---------------------------------------------------------------------------


class TestMergeCheckResult:
    def test_frozen(self) -> None:
        result = MergeCheckResult(
            task_id="T-1",
            source_branch="agent/backend-abc",
            target_branch="main",
            can_merge=True,
            conflict_files=[],
            merge_strategy="fast-forward",
            checked_at=datetime.now(UTC),
        )
        assert result.can_merge is True
        # frozen — assignment should raise
        try:
            result.can_merge = False  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass

    def test_fields(self) -> None:
        now = datetime.now(UTC)
        result = MergeCheckResult(
            task_id="T-42",
            source_branch="agent/qa-xyz",
            target_branch="main",
            can_merge=False,
            conflict_files=["src/auth.py"],
            merge_strategy="recursive",
            checked_at=now,
        )
        assert result.task_id == "T-42"
        assert result.source_branch == "agent/qa-xyz"
        assert result.target_branch == "main"
        assert result.can_merge is False
        assert result.conflict_files == ["src/auth.py"]
        assert result.merge_strategy == "recursive"
        assert result.checked_at == now

    def test_conflict_files_list(self) -> None:
        result = MergeCheckResult(
            task_id="T-1",
            source_branch="feat/a",
            target_branch="main",
            can_merge=False,
            conflict_files=["a.py", "b.py", "c.py"],
            merge_strategy="recursive",
            checked_at=datetime.now(UTC),
        )
        assert len(result.conflict_files) == 3
        assert "b.py" in result.conflict_files


# ---------------------------------------------------------------------------
# MergeAttempt
# ---------------------------------------------------------------------------


class TestMergeAttempt:
    def test_frozen(self) -> None:
        check = MergeCheckResult(
            task_id="T-1",
            source_branch="agent/a",
            target_branch="main",
            can_merge=True,
            conflict_files=[],
            merge_strategy="fast-forward",
            checked_at=datetime.now(UTC),
        )
        attempt = MergeAttempt(
            task_id="T-1",
            attempt_id="abc123",
            result=check,
            applied=False,
            error="",
        )
        try:
            attempt.applied = True  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass

    def test_fields(self) -> None:
        check = MergeCheckResult(
            task_id="T-5",
            source_branch="agent/b",
            target_branch="main",
            can_merge=True,
            conflict_files=[],
            merge_strategy="recursive",
            checked_at=datetime.now(UTC),
        )
        attempt = MergeAttempt(
            task_id="T-5",
            attempt_id="xyz789",
            result=check,
            applied=True,
            error="",
        )
        assert attempt.task_id == "T-5"
        assert attempt.attempt_id == "xyz789"
        assert attempt.result is check
        assert attempt.applied is True
        assert attempt.error == ""

    def test_error_field(self) -> None:
        check = MergeCheckResult(
            task_id="T-1",
            source_branch="agent/c",
            target_branch="main",
            can_merge=False,
            conflict_files=["x.py"],
            merge_strategy="recursive",
            checked_at=datetime.now(UTC),
        )
        attempt = MergeAttempt(
            task_id="T-1",
            attempt_id="err001",
            result=check,
            applied=False,
            error="merge conflict in x.py",
        )
        assert attempt.error == "merge conflict in x.py"
        assert attempt.applied is False


# ---------------------------------------------------------------------------
# dry_run_merge
# ---------------------------------------------------------------------------


class TestDryRunMerge:
    def test_returns_merge_check_result(self, tmp_path: Path) -> None:
        result = dry_run_merge("agent/backend-abc", "main", tmp_path, task_id="T-1")
        assert isinstance(result, MergeCheckResult)

    def test_can_merge_is_true(self, tmp_path: Path) -> None:
        result = dry_run_merge("agent/backend-abc", "main", tmp_path, task_id="T-1")
        assert result.can_merge is True

    def test_conflict_files_empty(self, tmp_path: Path) -> None:
        result = dry_run_merge("agent/backend-abc", "main", tmp_path, task_id="T-1")
        assert result.conflict_files == []

    def test_task_id_embedded(self, tmp_path: Path) -> None:
        result = dry_run_merge("feat/x", "main", tmp_path, task_id="T-99")
        assert result.task_id == "T-99"

    def test_branches_captured(self, tmp_path: Path) -> None:
        result = dry_run_merge("agent/qa-xyz", "develop", tmp_path)
        assert result.source_branch == "agent/qa-xyz"
        assert result.target_branch == "develop"

    def test_checked_at_is_utc(self, tmp_path: Path) -> None:
        before = datetime.now(UTC)
        result = dry_run_merge("feat/x", "main", tmp_path)
        after = datetime.now(UTC)
        assert before <= result.checked_at <= after

    def test_same_branch_uses_fast_forward(self, tmp_path: Path) -> None:
        result = dry_run_merge("main", "main", tmp_path)
        assert result.merge_strategy == "fast-forward"

    def test_diverged_branches_use_recursive(self, tmp_path: Path) -> None:
        result = dry_run_merge("agent/backend-abc", "origin/main", tmp_path)
        assert result.merge_strategy == "recursive"

    def test_simple_branch_uses_fast_forward(self, tmp_path: Path) -> None:
        result = dry_run_merge("feat-x", "main", tmp_path)
        assert result.merge_strategy == "fast-forward"

    def test_default_task_id_empty(self, tmp_path: Path) -> None:
        result = dry_run_merge("feat/a", "main", tmp_path)
        assert result.task_id == ""


# ---------------------------------------------------------------------------
# build_merge_command
# ---------------------------------------------------------------------------


class TestBuildMergeCommand:
    def test_fast_forward(self) -> None:
        cmd = build_merge_command("feat/x", "fast-forward")
        assert cmd == ["git", "merge", "--ff-only", "feat/x"]

    def test_recursive(self) -> None:
        cmd = build_merge_command("agent/backend-abc", "recursive")
        assert cmd == ["git", "merge", "--strategy", "recursive", "agent/backend-abc"]

    def test_octopus(self) -> None:
        cmd = build_merge_command("feat/multi", "octopus")
        assert cmd == ["git", "merge", "--strategy", "octopus", "feat/multi"]

    def test_source_is_last_arg(self) -> None:
        cmd = build_merge_command("my-branch", "fast-forward")
        assert cmd[-1] == "my-branch"

    def test_starts_with_git_merge(self) -> None:
        cmd = build_merge_command("x", "recursive")
        assert cmd[0] == "git"
        assert cmd[1] == "merge"


# ---------------------------------------------------------------------------
# should_attempt_merge
# ---------------------------------------------------------------------------


class TestShouldAttemptMerge:
    def _make_check(
        self,
        *,
        can_merge: bool = True,
        conflict_files: list[str] | None = None,
    ) -> MergeCheckResult:
        return MergeCheckResult(
            task_id="T-1",
            source_branch="agent/a",
            target_branch="main",
            can_merge=can_merge,
            conflict_files=conflict_files or [],
            merge_strategy="fast-forward",
            checked_at=datetime.now(UTC),
        )

    def test_clean_merge(self) -> None:
        assert should_attempt_merge(self._make_check()) is True

    def test_cannot_merge(self) -> None:
        assert should_attempt_merge(self._make_check(can_merge=False)) is False

    def test_has_conflicts(self) -> None:
        check = self._make_check(conflict_files=["a.py"])
        assert should_attempt_merge(check) is False

    def test_cannot_merge_with_conflicts(self) -> None:
        check = self._make_check(can_merge=False, conflict_files=["a.py"])
        assert should_attempt_merge(check) is False

    def test_can_merge_but_empty_conflicts(self) -> None:
        check = self._make_check(can_merge=True, conflict_files=[])
        assert should_attempt_merge(check) is True


# ---------------------------------------------------------------------------
# format_merge_check
# ---------------------------------------------------------------------------


class TestFormatMergeCheck:
    def _make_check(
        self,
        *,
        can_merge: bool = True,
        conflict_files: list[str] | None = None,
        task_id: str = "T-1",
    ) -> MergeCheckResult:
        return MergeCheckResult(
            task_id=task_id,
            source_branch="agent/backend-abc",
            target_branch="main",
            can_merge=can_merge,
            conflict_files=conflict_files or [],
            merge_strategy="recursive",
            checked_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        )

    def test_clean_status(self) -> None:
        output = format_merge_check(self._make_check())
        assert "[CLEAN]" in output

    def test_conflict_status(self) -> None:
        output = format_merge_check(self._make_check(can_merge=False, conflict_files=["a.py"]))
        assert "[CONFLICT]" in output

    def test_includes_task_id(self) -> None:
        output = format_merge_check(self._make_check(task_id="T-42"))
        assert "T-42" in output

    def test_includes_branches(self) -> None:
        output = format_merge_check(self._make_check())
        assert "agent/backend-abc" in output
        assert "main" in output

    def test_includes_strategy(self) -> None:
        output = format_merge_check(self._make_check())
        assert "recursive" in output

    def test_includes_timestamp(self) -> None:
        output = format_merge_check(self._make_check())
        assert "2026-04-10" in output

    def test_conflict_files_listed(self) -> None:
        output = format_merge_check(self._make_check(can_merge=False, conflict_files=["src/a.py", "src/b.py"]))
        assert "src/a.py" in output
        assert "src/b.py" in output
        assert "conflicts (2)" in output

    def test_no_conflict_section_when_clean(self) -> None:
        output = format_merge_check(self._make_check())
        assert "conflicts" not in output


# ---------------------------------------------------------------------------
# create_merge_attempt
# ---------------------------------------------------------------------------


class TestCreateMergeAttempt:
    def _make_check(self) -> MergeCheckResult:
        return MergeCheckResult(
            task_id="T-1",
            source_branch="agent/a",
            target_branch="main",
            can_merge=True,
            conflict_files=[],
            merge_strategy="fast-forward",
            checked_at=datetime.now(UTC),
        )

    def test_creates_attempt(self) -> None:
        check = self._make_check()
        attempt = create_merge_attempt(check, applied=True)
        assert isinstance(attempt, MergeAttempt)
        assert attempt.task_id == "T-1"
        assert attempt.applied is True
        assert attempt.error == ""
        assert attempt.result is check

    def test_attempt_id_generated(self) -> None:
        check = self._make_check()
        a1 = create_merge_attempt(check)
        a2 = create_merge_attempt(check)
        assert a1.attempt_id != a2.attempt_id
        assert len(a1.attempt_id) == 12

    def test_error_captured(self) -> None:
        check = self._make_check()
        attempt = create_merge_attempt(check, applied=False, error="conflict")
        assert attempt.error == "conflict"
        assert attempt.applied is False

    def test_defaults(self) -> None:
        check = self._make_check()
        attempt = create_merge_attempt(check)
        assert attempt.applied is False
        assert attempt.error == ""
