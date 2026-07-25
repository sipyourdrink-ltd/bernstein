"""Tests for janitor -- completion signal verification."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.janitor import (
    _extract_branch_ref,
    _get_judge_retry_count,
    _parse_judge_response,
    _resolve_branch_check_command,
    _resolve_branch_ref,
    create_fix_tasks,
    evaluate_signal,
    judge_task,
    run_janitor,
    verify_task,
)
from bernstein.core.models import CompletionSignal, Task, TaskType, UpgradeProposalDetails

if TYPE_CHECKING:
    from pathlib import Path

# --- Fixtures ---


def _make_task(
    *,
    id: str = "T-100",
    signals: list[CompletionSignal] | None = None,
) -> Task:
    return Task(
        id=id,
        title="Test task",
        description="A task for testing.",
        role="backend",
        completion_signals=signals or [],
    )


# --- path_exists ---


class TestPathExists:
    def test_passes_for_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "foo.py"
        target.write_text("print('hello')")

        signal = CompletionSignal(type="path_exists", value="foo.py")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_fails_for_missing_file(self, tmp_path: Path) -> None:
        signal = CompletionSignal(type="path_exists", value="missing.py")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_passes_for_directory(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()

        signal = CompletionSignal(type="path_exists", value="src")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_handles_absolute_path(self, tmp_path: Path) -> None:
        target = tmp_path / "abs.txt"
        target.write_text("data")

        signal = CompletionSignal(type="path_exists", value=str(target))
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True


class TestPathExistsGlobAndFuzzy:
    """Issue #2186: manager-guessed literal paths vs. worker-chosen
    repo-idiomatic paths. Conservative matching contract:
      - exact-path matching is the default, unchanged;
      - explicit glob syntax in the criterion always works (opt-in
        per-check by construction);
      - the fuzzy basename fallback is opt-in via
        BERNSTEIN_JANITOR_FUZZY_PATHS=1, default OFF."""

    _GUESSED = "packages/db/test/seed-workers.test.ts"

    def _write_idiomatic_file(self, tmp_path: Path) -> None:
        """Worker output at the repo's actual convention -- a fuzzy (but
        not literal or glob) match for the manager's guessed path."""
        actual_dir = tmp_path / "packages" / "db" / "src" / "__tests__"
        actual_dir.mkdir(parents=True)
        (actual_dir / "seed-workers-demo-link.test.ts").write_text("x")

    def test_literal_hit_unaffected(self, tmp_path: Path) -> None:
        """A literal hit keeps the original ("exists") detail string --
        no behavior change for the common case."""
        (tmp_path / "foo.py").write_text("x")

        signal = CompletionSignal(type="path_exists", value="foo.py")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_fuzzy_off_by_default(self, tmp_path: Path) -> None:
        """DEFAULT behavior: a literal miss FAILS even when a would-be
        fuzzy match exists -- exact-path matching is the contract
        operators rely on."""
        self._write_idiomatic_file(tmp_path)

        signal = CompletionSignal(type="path_exists", value=self._GUESSED)
        env = {k: v for k, v in os.environ.items() if k != "BERNSTEIN_JANITOR_FUZZY_PATHS"}
        with patch.dict("os.environ", env, clear=True):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_fuzzy_explicit_zero_also_off(self, tmp_path: Path) -> None:
        self._write_idiomatic_file(tmp_path)

        signal = CompletionSignal(type="path_exists", value=self._GUESSED)
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "0"}):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_explicit_glob_works_without_flag(self, tmp_path: Path) -> None:
        """Explicit glob syntax in the criterion is honored regardless of
        the fuzzy flag -- opt-in per-check by construction."""
        nested = tmp_path / "packages" / "db"
        nested.mkdir(parents=True)
        (nested / "seed-workers.test.ts").write_text("x")

        signal = CompletionSignal(type="path_exists", value="packages/*/seed-workers.test.ts")
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "0"}):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert "glob pattern matched" in detail
        assert "seed-workers.test.ts" in detail

    def test_explicit_glob_recursive_different_depth(self, tmp_path: Path) -> None:
        """A `**` glob written in the criterion matches at any depth,
        with the fuzzy flag off."""
        self._write_idiomatic_file(tmp_path)

        signal = CompletionSignal(type="path_exists", value="packages/db/**/seed-workers*.test.ts")
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "0"}):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert "seed-workers-demo-link.test.ts" in detail

    def test_explicit_glob_no_match_fails(self, tmp_path: Path) -> None:
        (tmp_path / "unrelated.txt").write_text("x")

        signal = CompletionSignal(type="path_exists", value="packages/**/seed-workers*.test.ts")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_fuzzy_opt_in_hits_at_different_depth(self, tmp_path: Path) -> None:
        """The exact #2186 repro with BERNSTEIN_JANITOR_FUZZY_PATHS=1:
        manager guesses `packages/db/test/...`, worker writes to
        `packages/db/src/__tests__/...` -- a different directory depth
        and name suffix. The opt-in fuzzy basename fallback finds it."""
        self._write_idiomatic_file(tmp_path)

        signal = CompletionSignal(type="path_exists", value=self._GUESSED)
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "1"}):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert "fuzzy fallback" in detail
        assert "seed-workers-demo-link.test.ts" in detail

    def test_fuzzy_opt_in_true_miss_still_fails(self, tmp_path: Path) -> None:
        """With fuzzy enabled, a path with no literal or fuzzy match
        anywhere under workdir must still fail -- the fallback is not a
        rubber stamp."""
        (tmp_path / "unrelated.txt").write_text("x")

        signal = CompletionSignal(type="path_exists", value=self._GUESSED)
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "1"}):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_fuzzy_match_logs_loudly_with_matched_path(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """When the fuzzy fallback satisfies a check it must WARN, naming
        the literal miss, the pattern, and the matched path (logging is
        the debugging interface -- see #2186 postmortem)."""
        self._write_idiomatic_file(tmp_path)

        signal = CompletionSignal(type="path_exists", value=self._GUESSED)
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "1"}), caplog.at_level("WARNING"):
            passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True
        fuzzy_warnings = [rec for rec in caplog.records if rec.levelname == "WARNING" and "FUZZY MATCH" in rec.message]
        assert fuzzy_warnings, "expected a WARNING log for the fuzzy match"
        assert any(self._GUESSED in rec.message for rec in fuzzy_warnings)
        assert any("seed-workers-demo-link.test.ts" in rec.message for rec in fuzzy_warnings)

    def test_glob_match_logs_matched_path(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        nested = tmp_path / "packages" / "db"
        nested.mkdir(parents=True)
        (nested / "seed-workers.test.ts").write_text("x")

        signal = CompletionSignal(type="path_exists", value="packages/*/seed-workers.test.ts")
        with caplog.at_level("INFO"):
            passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert any("glob pattern" in rec.message and "seed-workers.test.ts" in rec.message for rec in caplog.records)

    def test_fuzzy_true_miss_logs_patterns_tried(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        signal = CompletionSignal(type="path_exists", value=self._GUESSED)
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "1"}), caplog.at_level("INFO"):
            passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert any("no match" in rec.message and "seed-workers" in rec.message for rec in caplog.records)

    def test_literal_path_with_glob_metachar_matched_literally(self, tmp_path: Path) -> None:
        """A real file whose literal path contains a glob metacharacter (e.g.
        a Next.js dynamic-route file `app/users/[id]/page.tsx`) must be
        matched literally, NOT silently reinterpreted as a glob pattern that
        would fail to match its own brackets."""
        route_dir = tmp_path / "app" / "users" / "[id]"
        route_dir.mkdir(parents=True)
        (route_dir / "page.tsx").write_text("export default 1")

        signal = CompletionSignal(type="path_exists", value="app/users/[id]/page.tsx")
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "0"}):
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_literal_path_with_bracket_fixture_matched_literally(self, tmp_path: Path) -> None:
        """`foo[1].json` on disk must satisfy a literal `foo[1].json` check;
        glob would read `[1]` as a character class and miss the real file."""
        (tmp_path / "foo[1].json").write_text("{}")

        signal = CompletionSignal(type="path_exists", value="foo[1].json")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_glob_still_honored_when_literal_missing(self, tmp_path: Path) -> None:
        """When no literal file matches, a criterion with glob syntax still
        falls through to glob interpretation (opt-in per-check)."""
        nested = tmp_path / "packages" / "db"
        nested.mkdir(parents=True)
        (nested / "seed-workers.test.ts").write_text("x")

        signal = CompletionSignal(type="path_exists", value="packages/*/seed-workers.test.ts")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert "glob pattern matched" in detail


class TestWorktreeSignalDelivery:
    """Issue #2760: filesystem completion signals that point inside an
    ephemeral agent worktree (``.sdd/worktrees/<session>/`` or
    ``.sdd/runtime/worktrees/<session>/``) must be translated to their
    repo-relative form and verified against the operator checkout.
    Existence only in the worktree proves the agent wrote the file at
    some instant, not that merge-back delivered it -- the worktree is
    deleted after the run."""

    _SESSION = "manager-e2760abc"

    def _write_worktree_file(self, tmp_path: Path, rel: str, *, runtime_layout: bool = False) -> Path:
        """Create *rel* inside a fake agent worktree and return its path."""
        base = (".sdd", "runtime", "worktrees") if runtime_layout else (".sdd", "worktrees")
        target = tmp_path.joinpath(*base, self._SESSION, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("deliverable\n")
        return target

    def test_worktree_only_file_fails_relative_signal(self, tmp_path: Path) -> None:
        """The exact #2760 shape: the file exists ONLY in the worktree."""
        self._write_worktree_file(tmp_path, "hello.txt")

        signal = CompletionSignal(type="path_exists", value=f".sdd/worktrees/{self._SESSION}/hello.txt")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "worktree" in detail
        assert "hello.txt" in detail

    def test_worktree_only_file_fails_absolute_signal(self, tmp_path: Path) -> None:
        worktree_file = self._write_worktree_file(tmp_path, "hello.txt")

        signal = CompletionSignal(type="path_exists", value=str(worktree_file))
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "worktree" in detail

    def test_merged_file_passes(self, tmp_path: Path) -> None:
        """After merge-back delivered the file, the translated check passes."""
        self._write_worktree_file(tmp_path, "hello.txt")
        (tmp_path / "hello.txt").write_text("deliverable\n")

        signal = CompletionSignal(type="path_exists", value=f".sdd/worktrees/{self._SESSION}/hello.txt")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_merged_file_passes_after_worktree_deleted(self, tmp_path: Path) -> None:
        """Delivered artifact verifies even when the worktree is already gone."""
        (tmp_path / "hello.txt").write_text("deliverable\n")

        signal = CompletionSignal(type="path_exists", value=f".sdd/worktrees/{self._SESSION}/hello.txt")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_runtime_worktree_layout_also_translated(self, tmp_path: Path) -> None:
        self._write_worktree_file(tmp_path, "hello.txt", runtime_layout=True)

        rel = f".sdd/runtime/worktrees/{self._SESSION}/hello.txt"
        signal = CompletionSignal(type="path_exists", value=rel)
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "worktree" in detail

    def test_nested_deliverable_keeps_subdirectories(self, tmp_path: Path) -> None:
        self._write_worktree_file(tmp_path, "docs/report.md")
        delivered = tmp_path / "docs" / "report.md"
        delivered.parent.mkdir(parents=True)
        delivered.write_text("deliverable\n")

        rel = f".sdd/worktrees/{self._SESSION}/docs/report.md"
        signal = CompletionSignal(type="path_exists", value=rel)
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_gitignored_deliverable_names_cause(self, tmp_path: Path) -> None:
        """A miss whose path matches a .gitignore rule must say WHY the
        merge-back never delivered it."""
        _run_git(["init", "-q"], tmp_path)
        (tmp_path / ".gitignore").write_text("hello.txt\n")
        self._write_worktree_file(tmp_path, "hello.txt")

        signal = CompletionSignal(type="path_exists", value=f".sdd/worktrees/{self._SESSION}/hello.txt")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "gitignore" in detail
        assert "merge-back" in detail
        assert ".gitignore:1:hello.txt" in detail

    def test_plain_miss_detail_unchanged(self, tmp_path: Path) -> None:
        """A miss outside any worktree keeps the original wire format."""
        signal = CompletionSignal(type="path_exists", value="missing.txt")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_non_worktree_sdd_path_not_translated(self, tmp_path: Path) -> None:
        """Other .sdd paths (backlog, metrics, ...) are untouched."""
        target = tmp_path / ".sdd" / "backlog" / "open" / "t.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("x")

        signal = CompletionSignal(type="path_exists", value=".sdd/backlog/open/t.yaml")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_glob_fallback_ignores_worktree_matches(self, tmp_path: Path) -> None:
        """A path_exists glob must not be satisfied by worktree-internal files."""
        self._write_worktree_file(tmp_path, "hello.txt")

        signal = CompletionSignal(type="path_exists", value="**/hello.txt")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fuzzy_fallback_ignores_worktree_matches(self, tmp_path: Path) -> None:
        self._write_worktree_file(tmp_path, "hello-final.txt")

        signal = CompletionSignal(type="path_exists", value="hello.txt")
        with patch.dict("os.environ", {"BERNSTEIN_JANITOR_FUZZY_PATHS": "1"}):
            passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_glob_exists_signal_ignores_worktree_matches(self, tmp_path: Path) -> None:
        self._write_worktree_file(tmp_path, "hello.txt")

        signal = CompletionSignal(type="glob_exists", value="**/hello.txt")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_file_contains_checks_operator_copy(self, tmp_path: Path) -> None:
        self._write_worktree_file(tmp_path, "hello.txt")

        value = f".sdd/worktrees/{self._SESSION}/hello.txt :: deliverable"
        signal = CompletionSignal(type="file_contains", value=value)
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

        (tmp_path / "hello.txt").write_text("deliverable\n")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_verify_task_failure_carries_actionable_detail(self, tmp_path: Path) -> None:
        """The failed-signal description surfaced to retry/fail paths names
        the worktree cause, not just the raw signal value."""
        self._write_worktree_file(tmp_path, "hello.txt")
        task = _make_task(
            signals=[
                CompletionSignal(
                    type="path_exists",
                    value=f".sdd/worktrees/{self._SESSION}/hello.txt",
                )
            ]
        )

        passed, failed = verify_task(task, tmp_path)
        assert passed is False
        assert len(failed) == 1
        assert "worktree" in failed[0]


# --- glob_exists ---


class TestGlobExists:
    def test_passes_when_files_match(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")

        signal = CompletionSignal(type="glob_exists", value="*.py")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_fails_when_no_match(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")

        signal = CompletionSignal(type="glob_exists", value="*.py")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_recursive_glob(self, tmp_path: Path) -> None:
        nested = tmp_path / "src" / "pkg"
        nested.mkdir(parents=True)
        (nested / "module.py").write_text("pass")

        signal = CompletionSignal(type="glob_exists", value="**/*.py")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True


# --- test_passes ---


class TestTestPasses:
    def test_passes_on_exit_zero(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="test_passes",
            value=f'{sys.executable} -c "raise SystemExit(0)"',
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_fails_on_nonzero_exit(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="test_passes",
            value=f'{sys.executable} -c "raise SystemExit(1)"',
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fails_on_bad_command(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="test_passes",
            value="nonexistent_command_xyz_12345",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_nonzero_exit_logs_failure_detail(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Logging gap: a failing test_passes command used to fail silently
        (no log line at all).  Assert the FAIL log line fires with the
        command, exit code, and captured stderr/stdout."""
        caplog.set_level("INFO", logger="bernstein.core.quality.janitor")
        signal = CompletionSignal(
            type="test_passes",
            value=f"{sys.executable} -c \"import sys; sys.stderr.write('boom'); raise SystemExit(1)\"",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False
        fail_records = [r for r in caplog.records if "test_passes FAIL" in r.message]
        assert fail_records, f"expected a test_passes FAIL log line, got: {[r.message for r in caplog.records]}"
        assert "exit=1" in fail_records[0].message
        assert "boom" in fail_records[0].message


# --- bug 12: branch-check acceptance signals vs actual pushed branch ---
#
# Regression coverage for work/agent-reports/2026-07-02-run9-attempt9-audit.md
# (task d56c18f2fc08): the janitor's acceptance signal expected the branch
# ``fix-905-demo-worker-record`` (hyphens) but the agent actually pushed
# ``fix/905-demo-worker-record`` (slash) -- a real, delivered PR was recorded
# as a DLQ'd failure.


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _init_git_repo_with_branch(tmp_path: Path, branch_name: str) -> Path:
    """Create a throwaway git repo at *tmp_path* with a commit on *branch_name*."""
    _run_git(["init", "-q"], tmp_path)
    _run_git(["config", "user.email", "test@example.com"], tmp_path)
    _run_git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "README.md").write_text("seed\n")
    _run_git(["add", "README.md"], tmp_path)
    _run_git(["commit", "-q", "-m", "seed"], tmp_path)
    _run_git(["checkout", "-q", "-b", branch_name], tmp_path)
    (tmp_path / "work.txt").write_text("work\n")
    _run_git(["add", "work.txt"], tmp_path)
    _run_git(["commit", "-q", "-m", "fix: demo worker record (#905)"], tmp_path)
    _run_git(["checkout", "-q", "-"], tmp_path)  # back to the initial branch
    return tmp_path


class TestExtractBranchRef:
    def test_extracts_ref_from_rev_parse_verify(self) -> None:
        assert _extract_branch_ref("git rev-parse --verify fix-905-demo-worker-record") == "fix-905-demo-worker-record"

    def test_extracts_ref_from_git_log_with_pipe(self) -> None:
        command = "git log -1 --format=%s fix-905-demo-worker-record | grep -q '#905'"
        assert _extract_branch_ref(command) == "fix-905-demo-worker-record"

    def test_ignores_special_refs(self) -> None:
        assert _extract_branch_ref("git rev-parse --verify HEAD") is None
        assert _extract_branch_ref("git rev-parse --verify main") is None

    def test_ignores_non_branch_commands(self) -> None:
        assert _extract_branch_ref("pytest tests/unit/test_foo.py -x") is None


class TestResolveBranchRef:
    def test_resolves_hyphenated_expectation_to_slash_branch(self, tmp_path: Path) -> None:
        # Signal expects hyphens; agent actually pushed with a slash.
        _init_git_repo_with_branch(tmp_path, "fix/905-demo-worker-record")
        resolved, branches = _resolve_branch_ref("fix-905-demo-worker-record", tmp_path)
        assert resolved == "fix/905-demo-worker-record"
        assert "fix/905-demo-worker-record" in branches

    def test_resolves_slash_expectation_to_hyphen_branch(self, tmp_path: Path) -> None:
        # The reverse drift direction also resolves.
        _init_git_repo_with_branch(tmp_path, "fix-905-demo-worker-record")
        resolved, _ = _resolve_branch_ref("fix/905-demo-worker-record", tmp_path)
        assert resolved == "fix-905-demo-worker-record"

    def test_exact_match_wins_without_fuzzing(self, tmp_path: Path) -> None:
        _init_git_repo_with_branch(tmp_path, "fix-905-demo-worker-record")
        resolved, _ = _resolve_branch_ref("fix-905-demo-worker-record", tmp_path)
        assert resolved == "fix-905-demo-worker-record"

    def test_returns_none_when_branch_truly_absent(self, tmp_path: Path) -> None:
        _init_git_repo_with_branch(tmp_path, "feat/unrelated-branch")
        resolved, branches = _resolve_branch_ref("fix-905-demo-worker-record", tmp_path)
        assert resolved is None
        assert "feat/unrelated-branch" in branches


class TestBranchCheckAcceptanceEndToEnd:
    """Exercises the actual bug-12 scenario through evaluate_signal()."""

    def test_janitor_accepts_slash_branch_when_signal_expects_hyphens(self, tmp_path: Path) -> None:
        _init_git_repo_with_branch(tmp_path, "fix/905-demo-worker-record")
        signal = CompletionSignal(
            type="test_passes",
            value="git rev-parse --verify fix-905-demo-worker-record",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_janitor_accepts_commit_message_check_across_naming_drift(self, tmp_path: Path) -> None:
        _init_git_repo_with_branch(tmp_path, "fix/905-demo-worker-record")
        signal = CompletionSignal(
            type="test_passes",
            value="git log -1 --format=%s fix-905-demo-worker-record | grep -q '#905'",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_janitor_still_fails_when_branch_never_pushed(self, tmp_path: Path) -> None:
        _init_git_repo_with_branch(tmp_path, "feat/unrelated-branch")
        signal = CompletionSignal(
            type="test_passes",
            value="git rev-parse --verify fix-905-demo-worker-record",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_logs_expected_and_found_branches_with_verdict(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _init_git_repo_with_branch(tmp_path, "fix/905-demo-worker-record")
        signal = CompletionSignal(
            type="test_passes",
            value="git rev-parse --verify fix-905-demo-worker-record",
        )
        with caplog.at_level("INFO", logger="bernstein.core.quality.janitor"):
            evaluate_signal(signal, tmp_path)
        combined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "fix-905-demo-worker-record" in combined
        assert "fix/905-demo-worker-record" in combined
        assert "rewriting command" in combined

    def test_resolve_branch_check_command_is_noop_for_non_git_commands(self, tmp_path: Path) -> None:
        command = f'{sys.executable} -c "raise SystemExit(0)"'
        assert _resolve_branch_check_command(command, tmp_path) == command


# --- file_contains ---


class TestFileContains:
    def test_passes_when_needle_present(self, tmp_path: Path) -> None:
        target = tmp_path / "module.py"
        target.write_text("class Foo:\n    pass\n")

        signal = CompletionSignal(
            type="file_contains",
            value="module.py :: class Foo",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_fails_when_needle_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "module.py"
        target.write_text("class Bar:\n    pass\n")

        signal = CompletionSignal(
            type="file_contains",
            value="module.py :: class Foo",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fails_for_missing_file(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="file_contains",
            value="missing.py :: class Foo",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fails_on_bad_format(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="file_contains",
            value="no separator here",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_splits_on_first_separator_only(self, tmp_path: Path) -> None:
        """Needle itself can contain ' :: '."""
        target = tmp_path / "data.txt"
        target.write_text("key :: value :: extra")

        signal = CompletionSignal(
            type="file_contains",
            value="data.txt :: value :: extra",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True


# --- llm_review ---


class TestLlmReview:
    def test_passes_on_pass_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="PASS: All error handling looks good\n",
            stderr="",
        )
        with patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check error handling")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert "error handling" in detail.lower()

    def test_fails_on_fail_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="FAIL: Missing input validation on POST endpoint\n",
            stderr="",
        )
        with patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check input validation")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "input validation" in detail.lower()

    def test_fails_on_empty_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "empty" in detail

    def test_fails_on_ambiguous_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="I think it looks okay maybe\n",
            stderr="",
        )
        with patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "ambiguous" in detail

    def test_fails_on_timeout(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.janitor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60),
        ):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "timed out" in detail

    def test_fails_on_missing_cli(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.janitor.subprocess.run",
            side_effect=FileNotFoundError("claude not found"),
        ):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "spawn" in detail

    def test_spawns_correct_command(self, tmp_path: Path) -> None:
        """Verify the exact CLI arguments passed to subprocess."""
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="PASS: looks good\n",
            stderr="",
        )
        with patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_result) as mock_run:
            signal = CompletionSignal(type="llm_review", value="Check error handling")
            evaluate_signal(signal, tmp_path)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]  # positional arg
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        assert "Check error handling" in cmd[2]
        assert cmd[3] == "--model"
        assert cmd[4] == "sonnet"
        assert call_args[1]["timeout"] == 60
        assert call_args[1]["cwd"] == tmp_path


# --- verify_task ---


class TestVerifyTask:
    def test_all_signals_pass(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("class Impl:\n    pass")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="a.py"),
                CompletionSignal(type="file_contains", value="a.py :: class Impl"),
            ]
        )

        passed, failed = verify_task(task, tmp_path)
        assert passed is True
        assert failed == []

    def test_partial_failure(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("class Impl:\n    pass")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="a.py"),
                CompletionSignal(type="path_exists", value="b.py"),
            ]
        )

        passed, failed = verify_task(task, tmp_path)
        assert passed is False
        assert len(failed) == 1
        assert "b.py" in failed[0]

    def test_no_signals_means_pass(self, tmp_path: Path) -> None:
        task = _make_task(signals=[])
        passed, failed = verify_task(task, tmp_path)
        assert passed is True
        assert failed == []


# --- run_janitor (async) ---


class TestRunJanitor:
    @pytest.mark.asyncio
    async def test_returns_results_for_evaluated_tasks(self, tmp_path: Path) -> None:
        (tmp_path / "done.py").write_text("pass")

        t1 = _make_task(
            id="T-001",
            signals=[CompletionSignal(type="path_exists", value="done.py")],
        )
        t2 = _make_task(
            id="T-002",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )
        results = await run_janitor([t1, t2], tmp_path)

        assert len(results) == 2
        assert results[0].task_id == "T-001"
        assert results[0].passed is True
        assert results[1].task_id == "T-002"
        assert results[1].passed is False

    @pytest.mark.asyncio
    async def test_skips_tasks_without_signals(self, tmp_path: Path) -> None:
        t1 = _make_task(id="T-001", signals=[])
        t2 = _make_task(
            id="T-002",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )
        results = await run_janitor([t1, t2], tmp_path)

        # T-001 has no signals so it is skipped
        assert len(results) == 1
        assert results[0].task_id == "T-002"

    @pytest.mark.asyncio
    async def test_empty_task_list(self, tmp_path: Path) -> None:
        results = await run_janitor([], tmp_path)
        assert results == []

    @pytest.mark.asyncio
    async def test_all_pass(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")

        t1 = _make_task(
            id="T-001",
            signals=[CompletionSignal(type="path_exists", value="a.py")],
        )
        t2 = _make_task(
            id="T-002",
            signals=[CompletionSignal(type="path_exists", value="b.py")],
        )
        results = await run_janitor([t1, t2], tmp_path)
        assert len(results) == 2
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_signal_results_structure(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")

        task = _make_task(
            id="T-001",
            signals=[
                CompletionSignal(type="path_exists", value="a.py"),
                CompletionSignal(type="path_exists", value="missing.py"),
            ],
        )
        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        sr = results[0].signal_results
        assert len(sr) == 2
        assert sr[0] == ("path_exists: a.py", True, "exists")
        assert sr[1] == ("path_exists: missing.py", False, "not found")

    @pytest.mark.asyncio
    async def test_accept_and_reject_verdicts_are_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logging gap: run_janitor's accept/reject disposition for each task
        was only visible via the returned JanitorResult, not the logs. Assert
        an ACCEPT line for the passing task and a REJECT line (with the
        failed signals) for the failing task."""
        caplog.set_level("INFO", logger="bernstein.core.quality.janitor")
        (tmp_path / "a.py").write_text("x")

        t_pass = _make_task(id="T-PASS", signals=[CompletionSignal(type="path_exists", value="a.py")])
        t_fail = _make_task(id="T-FAIL", signals=[CompletionSignal(type="path_exists", value="missing.py")])
        await run_janitor([t_pass, t_fail], tmp_path)

        accept_records = [r.message for r in caplog.records if "janitor ACCEPT" in r.message]
        reject_records = [r.message for r in caplog.records if "janitor REJECT" in r.message]
        assert any("T-PASS" in m for m in accept_records), accept_records
        assert any("T-FAIL" in m and "missing.py" in m for m in reject_records), reject_records


# --- create_fix_tasks ---


class TestCreateFixTasks:
    @pytest.mark.asyncio
    async def test_posts_to_server_and_returns_id(self, tmp_path: Path) -> None:
        import httpx

        task = _make_task(id="T-FAIL", signals=[])

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            await asyncio.sleep(0)  # Async interface requirement
            assert "/tasks" in url
            assert "Fix:" in json["title"]
            assert "T-FAIL" in json["description"]
            return httpx.Response(
                status_code=201,
                json={"id": "fix-001"},
                request=httpx.Request("POST", url),
            )

        with patch.object(httpx.AsyncClient, "post", mock_post):
            ids = await create_fix_tasks(task, ["path_exists: missing.py"], "http://localhost:8052")

        assert ids == ["fix-001"]

    @pytest.mark.asyncio
    async def test_handles_server_error_gracefully(self, tmp_path: Path) -> None:
        import httpx

        task = _make_task(id="T-FAIL", signals=[])

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            await asyncio.sleep(0)  # Async interface requirement
            return httpx.Response(
                status_code=500,
                text="Internal Server Error",
                request=httpx.Request("POST", url),
            )

        with patch.object(httpx.AsyncClient, "post", mock_post):
            ids = await create_fix_tasks(task, ["path_exists: missing.py"], "http://localhost:8052")

        assert ids == []

    @pytest.mark.asyncio
    async def test_run_janitor_creates_fix_tasks_on_failure(self, tmp_path: Path) -> None:
        import httpx

        task = _make_task(
            id="T-BAD",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            await asyncio.sleep(0)  # Async interface requirement
            return httpx.Response(
                status_code=201,
                json={"id": "fix-auto-001"},
                request=httpx.Request("POST", url),
            )

        with patch.object(httpx.AsyncClient, "post", mock_post):
            results = await run_janitor([task], tmp_path, server_url="http://localhost:8052")

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].fix_tasks_created == ["fix-auto-001"]

    @pytest.mark.asyncio
    async def test_run_janitor_no_fix_tasks_when_no_server(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-BAD",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )
        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].fix_tasks_created == []

    @pytest.mark.asyncio
    async def test_run_janitor_no_fix_tasks_when_all_pass(self, tmp_path: Path) -> None:
        (tmp_path / "exists.py").write_text("x")
        task = _make_task(
            id="T-OK",
            signals=[CompletionSignal(type="path_exists", value="exists.py")],
        )
        results = await run_janitor([task], tmp_path, server_url="http://localhost:8052")

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].fix_tasks_created == []


# --- _parse_judge_response ---


class TestParseJudgeResponse:
    def test_parses_accept_verdict(self) -> None:
        raw = '{"verdict": "accept", "confidence": 0.95, "feedback": "Looks good."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "accept"
        assert v.confidence == pytest.approx(0.95)
        assert v.feedback == "Looks good."
        assert v.flagged_for_review is False

    def test_parses_retry_verdict(self) -> None:
        raw = '{"verdict": "retry", "confidence": 0.8, "feedback": "Missing tests."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"
        assert v.confidence == pytest.approx(0.8)
        assert v.feedback == "Missing tests."
        assert v.flagged_for_review is False

    def test_flags_low_confidence(self) -> None:
        raw = '{"verdict": "accept", "confidence": 0.5, "feedback": "Unsure."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "accept"
        assert v.confidence == pytest.approx(0.5)
        assert v.flagged_for_review is True

    def test_handles_markdown_fences(self) -> None:
        raw = '```json\n{"verdict": "accept", "confidence": 0.9, "feedback": "OK"}\n```'
        v = _parse_judge_response(raw)
        assert v.verdict == "accept"
        assert v.confidence == pytest.approx(0.9)

    def test_extracts_json_from_surrounding_text(self) -> None:
        raw = 'Here is my response: {"verdict": "retry", "confidence": 0.6, "feedback": "Fix X"} done.'
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"
        assert v.feedback == "Fix X"

    def test_returns_retry_on_invalid_json(self) -> None:
        raw = "This is not JSON at all"
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"
        assert v.confidence == pytest.approx(0.0)
        assert v.flagged_for_review is True

    def test_clamps_confidence_to_bounds(self) -> None:
        raw = '{"verdict": "accept", "confidence": 1.5, "feedback": ""}'
        v = _parse_judge_response(raw)
        assert v.confidence == pytest.approx(1.0)

        raw2 = '{"verdict": "accept", "confidence": -0.3, "feedback": ""}'
        v2 = _parse_judge_response(raw2)
        assert v2.confidence == pytest.approx(0.0)

    def test_normalizes_unknown_verdict_to_retry(self) -> None:
        raw = '{"verdict": "maybe", "confidence": 0.8, "feedback": "Not sure."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"


# --- _get_judge_retry_count ---


class TestGetJudgeRetryCount:
    def test_returns_zero_for_normal_task(self) -> None:
        task = _make_task(id="T-1")
        assert _get_judge_retry_count(task) == 0

    def test_extracts_retry_count_from_description(self) -> None:
        task = Task(
            id="T-2",
            title="Fix: something",
            description="[judge_retry:2] Auto-created by LLM judge.\nOriginal task...",
            role="backend",
        )
        assert _get_judge_retry_count(task) == 2


# --- judge_task ---


class TestJudgeTask:
    @pytest.mark.asyncio
    async def test_accept_verdict(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-JUDGE-1",
            signals=[CompletionSignal(type="llm_judge", value="Check correctness")],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff --git a/foo.py\n+pass\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "accept", "confidence": 0.95, "feedback": "All good."}'

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check correctness")

        assert verdict.verdict == "accept"
        assert verdict.confidence == pytest.approx(0.95)
        assert verdict.flagged_for_review is False

    @pytest.mark.asyncio
    async def test_retry_verdict(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-JUDGE-2",
            signals=[CompletionSignal(type="llm_judge", value="Check tests")],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "retry", "confidence": 0.8, "feedback": "Missing unit tests."}'

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check tests")

        assert verdict.verdict == "retry"
        assert verdict.feedback == "Missing unit tests."

    @pytest.mark.asyncio
    async def test_low_confidence_flagged(self, tmp_path: Path) -> None:
        task = _make_task(id="T-JUDGE-3", signals=[])

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "accept", "confidence": 0.4, "feedback": "Not sure."}'

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check something")

        assert verdict.verdict == "accept"
        assert verdict.confidence == pytest.approx(0.4)
        assert verdict.flagged_for_review is True

    @pytest.mark.asyncio
    async def test_llm_failure_returns_retry_flagged(self, tmp_path: Path) -> None:
        task = _make_task(id="T-JUDGE-4", signals=[])

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch(
                "bernstein.core.janitor.call_llm",
                side_effect=RuntimeError("API error"),
            ),
        ):
            verdict = await judge_task(task, tmp_path, "Check something")

        assert verdict.verdict == "retry"
        assert verdict.confidence == pytest.approx(0.0)
        assert verdict.flagged_for_review is True
        assert "API error" in verdict.feedback

    @pytest.mark.asyncio
    async def test_empty_response_returns_retry_flagged(self, tmp_path: Path) -> None:
        task = _make_task(id="T-JUDGE-5", signals=[])

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return ""

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check something")

        assert verdict.verdict == "retry"
        assert verdict.confidence == pytest.approx(0.0)
        assert verdict.flagged_for_review is True


# --- run_janitor with llm_judge ---


class TestRunJanitorWithJudge:
    @pytest.mark.asyncio
    async def test_judge_accept_passes_task(self, tmp_path: Path) -> None:
        (tmp_path / "impl.py").write_text("class Foo: pass")
        task = _make_task(
            id="T-J-OK",
            signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check implementation"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "accept", "confidence": 0.9, "feedback": "Good."}'

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].judge_verdict is not None
        assert results[0].judge_verdict.verdict == "accept"

    @pytest.mark.asyncio
    async def test_judge_retry_creates_fix_task(self, tmp_path: Path) -> None:
        import httpx

        (tmp_path / "impl.py").write_text("pass")
        task = _make_task(
            id="T-J-RETRY",
            signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check impl"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "retry", "confidence": 0.8, "feedback": "Missing error handling."}'

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            await asyncio.sleep(0)  # Async interface requirement
            assert "judge retry 1" in json["title"]
            assert "[judge_retry:1]" in json["description"]
            assert "Missing error handling" in json["description"]
            return httpx.Response(
                status_code=201,
                json={"id": "fix-judge-001"},
                request=httpx.Request("POST", url),
            )

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
            patch.object(httpx.AsyncClient, "post", mock_post),
        ):
            results = await run_janitor(
                [task],
                tmp_path,
                server_url="http://localhost:8052",
            )

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].fix_tasks_created == ["fix-judge-001"]
        assert results[0].judge_verdict is not None
        assert results[0].judge_verdict.verdict == "retry"

    @pytest.mark.asyncio
    async def test_judge_skipped_when_non_judge_signals_fail(self, tmp_path: Path) -> None:
        """Judge should not run if prerequisite (non-judge) signals fail."""
        task = _make_task(
            id="T-J-SKIP",
            signals=[
                CompletionSignal(type="path_exists", value="missing.py"),
                CompletionSignal(type="llm_judge", value="Check impl"),
            ],
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        # Judge should be in signal_results as skipped
        judge_signals = [sr for sr in results[0].signal_results if sr[0].startswith("llm_judge")]
        assert len(judge_signals) == 1
        assert judge_signals[0][1] is False
        assert "skipped" in judge_signals[0][2]
        # No judge verdict since it was skipped
        assert results[0].judge_verdict is None

    @pytest.mark.asyncio
    async def test_max_retries_prevents_fix_task(self, tmp_path: Path) -> None:
        """After max retries, no more fix tasks should be created."""
        (tmp_path / "impl.py").write_text("pass")
        task = Task(
            id="T-J-MAX",
            title="Fix: something (judge retry 2)",
            description="[judge_retry:2] Previous retry...",
            role="backend",
            completion_signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check impl"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "retry", "confidence": 0.8, "feedback": "Still broken."}'

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            results = await run_janitor(
                [task],
                tmp_path,
                server_url="http://localhost:8052",
            )

        assert len(results) == 1
        assert results[0].passed is False
        # No fix tasks created because max retries exceeded
        assert results[0].fix_tasks_created == []

    @pytest.mark.asyncio
    async def test_judge_low_confidence_flagged_in_result(self, tmp_path: Path) -> None:
        (tmp_path / "impl.py").write_text("pass")
        task = _make_task(
            id="T-J-LOW",
            signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check quality"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            await asyncio.sleep(0)  # Async interface requirement
            return '{"verdict": "accept", "confidence": 0.5, "feedback": "Looks OK-ish."}'

        with (
            patch("bernstein.core.quality.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.quality.janitor.call_llm", side_effect=mock_call_llm),
        ):
            results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].judge_verdict is not None
        assert results[0].judge_verdict.flagged_for_review is True


# --- Empty-diff guard + attribution (item 15 / S2 family) ---


def _init_git_repo(repo: Path) -> None:
    """Create a git repo with one baseline commit (base.txt)."""

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "janitor-test@example.com")
    _git("config", "user.name", "Janitor Test")
    (repo / "base.txt").write_text("baseline\n")
    _git("add", "base.txt")
    _git("commit", "-q", "-m", "baseline commit")


class TestEmptyDiffGuardAndAttribution:
    """Regressions for the archived misattribution run (attempt-e938bd33):
    janitor_passed=true on a 0-file manager task while the worker with the
    real commit was rejected; S2 orphans with empty diffs rubber-stamped.
    """

    @pytest.mark.asyncio
    async def test_manager_zero_file_task_rejected(self, tmp_path: Path) -> None:
        """A task with no commits and no owned_files must NOT pass, even if
        its signals are satisfied by repo state another task created."""
        _init_git_repo(tmp_path)
        task = _make_task(
            id="MGR-0FILE",
            signals=[CompletionSignal(type="path_exists", value="base.txt")],
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        empty_diff = [d for d, ok, _ in results[0].signal_results if d == "attribution:empty_diff"]
        assert empty_diff, f"expected attribution:empty_diff signal, got {results[0].signal_results}"

    @pytest.mark.asyncio
    async def test_worker_with_real_commit_accepted(self, tmp_path: Path) -> None:
        """A worker whose commit message references its task id (the b9574d9
        pattern: '[WIP] qa-5a36fe2a partial work') is attributed and accepted."""
        _init_git_repo(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "cli.py").write_text("print('hello, world')\n")
        subprocess.run(["git", "add", "src/cli.py"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "[WIP] WKR-REAL partial work"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        task = Task(
            id="WKR-REAL",
            title="Implement hello CLI",
            description="Add cli.py with a hello command.",
            role="backend",
            completion_signals=[CompletionSignal(type="path_exists", value="src/cli.py")],
            owned_files=["src/cli.py"],
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True, f"signal_results={results[0].signal_results}"

    @pytest.mark.asyncio
    async def test_crash_recovery_orphan_empty_diff_rejected(self, tmp_path: Path) -> None:
        """S2 family: crash-recovery auto-completion with owned_files it never
        touched (empty diff, no commits) must be rejected, not rubber-stamped."""
        _init_git_repo(tmp_path)
        task = Task(
            id="ORPHAN-S2",
            title="Orphan recovered after crash",
            description="Auto-completed by crash recovery; no work happened.",
            role="backend",
            completion_signals=[CompletionSignal(type="path_exists", value="base.txt")],
            owned_files=["orphan.py"],
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        failed = [d for d, ok, _ in results[0].signal_results if not ok]
        assert any("empty_diff" in d for d in failed), f"failed={failed}"

    @pytest.mark.asyncio
    async def test_unattributable_diff_with_nontrivial_signal_accepted_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A task that landed real work in a commit whose message omits the
        task id and which has no owned_files (attribution returns empty) must
        NOT be hard-rejected when it has a passing non-trivial completion
        signal (a passing test_passes). Instead it is accepted and flagged for
        review, so real completions are not false-rejected just because the
        commit did not stamp the task id."""
        _init_git_repo(tmp_path)
        # A real landing commit whose message does NOT reference the task id.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "feature.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "add", "src/feature.py"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add feature"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        task = _make_task(
            id="NOSTAMP-1",
            signals=[
                CompletionSignal(
                    type="test_passes",
                    value=f'{sys.executable} -c "raise SystemExit(0)"',
                )
            ],
        )

        with caplog.at_level("WARNING"):
            results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True, f"signal_results={results[0].signal_results}"
        # Not hard-rejected: no failing empty_diff signal.
        failed = [d for d, ok, _ in results[0].signal_results if not ok]
        assert not any("empty_diff" in d and "warn" not in d for d in failed), f"failed={failed}"
        assert any("empty diff, flagged for review" in rec.message for rec in caplog.records), (
            "expected an empty-diff WARN log flagging the task for review"
        )

    @pytest.mark.asyncio
    async def test_research_noop_task_type_exempt(self, tmp_path: Path) -> None:
        """Explicit no-op task types (research) legitimately have no diff."""
        _init_git_repo(tmp_path)
        task = Task(
            id="RSRCH-1",
            title="Research task",
            description="Exploration; output lives in notes, not the repo.",
            role="backend",
            completion_signals=[CompletionSignal(type="path_exists", value="base.txt")],
            task_type=TaskType.RESEARCH,
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_upgrade_proposal_task_exempt_from_empty_diff_guard(self, tmp_path: Path) -> None:
        """UPGRADE_PROPOSAL tasks are verified by verify_upgrade_task(), which
        does not require owned_files -- UpgradeProposal.to_task() never sets
        owned_files, so the generic empty-diff attribution guard would
        otherwise hard-reject an already-verified upgrade proposal that made
        no repo changes of its own (e.g. an analysis-only proposal). This
        regression covers CodeRabbit's janitor.py:72 finding."""
        _init_git_repo(tmp_path)
        task = Task(
            id="UPGRADE-1",
            title="Upgrade proposal",
            description="Propose an upgrade; verified via verify_upgrade_task, not a diff.",
            role="backend",
            task_type=TaskType.UPGRADE_PROPOSAL,
            completion_signals=[CompletionSignal(type="path_exists", value="base.txt")],
            upgrade_details=UpgradeProposalDetails(),
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True, f"signal_results={results[0].signal_results}"
        failed = [d for d, ok, _ in results[0].signal_results if not ok]
        assert not any("empty_diff" in d for d in failed), f"failed={failed}"

    @pytest.mark.asyncio
    async def test_non_git_workdir_guard_skipped(self, tmp_path: Path) -> None:
        """Outside a git repo, attribution is impossible; signals-only
        judgment (historical behavior) is preserved."""
        task = _make_task(
            id="NOGIT-1",
            signals=[CompletionSignal(type="path_exists", value="plain.txt")],
        )
        (tmp_path / "plain.txt").write_text("x")

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True


class TestArtifactSignals:
    """Issue #2608: the three artifact-mode criteria and their janitor wiring.

    The filesystem ``evaluate_signal`` recognises the new types (never "unknown
    signal type") but defers them; ``evaluate_artifact_signals`` evaluates them
    against the produced artifact using the task's declared kind.
    """

    def test_evaluate_signal_defers_artifact_types_not_unknown(self, tmp_path: Path) -> None:
        for sig_type in ("schema_valid", "criteria_match", "hash_stable"):
            signal = CompletionSignal(type=sig_type, value="x")
            passed, detail = evaluate_signal(signal, tmp_path)
            assert passed is False
            assert "unknown signal type" not in detail
            assert "artifact-mode" in detail

    def test_hash_stable_signal_passes_on_matching_artifact(self) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals
        from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec, artifact_content_hash

        rows = [{"id": 1}, {"id": 2}]
        digest = artifact_content_hash(ArtifactKind.DATASET, rows)
        task = _make_task(signals=[CompletionSignal(type="hash_stable", value=digest)])
        task.artifact_spec = ArtifactSpec(kind=ArtifactKind.DATASET)

        results = evaluate_artifact_signals(task, rows)
        assert len(results) == 1
        _desc, passed, _detail = results[0]
        assert passed is True

    def test_hash_stable_signal_fails_on_mutated_artifact(self) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals
        from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec, artifact_content_hash

        digest = artifact_content_hash(ArtifactKind.REPORT, "the report\n")
        task = _make_task(signals=[CompletionSignal(type="hash_stable", value=digest)])
        task.artifact_spec = ArtifactSpec(kind=ArtifactKind.REPORT)

        results = evaluate_artifact_signals(task, "a different report\n")
        _desc, passed, detail = results[0]
        assert passed is False
        assert "drift" in detail

    def test_schema_valid_and_criteria_match_signals(self) -> None:
        import json

        from bernstein.core.quality.janitor import evaluate_artifact_signals
        from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec

        schema = json.dumps({"type": "object", "required": ["status"]})
        preds = json.dumps([{"path": "status", "op": "eq", "value": "ok"}])
        task = _make_task(
            signals=[
                CompletionSignal(type="schema_valid", value=schema),
                CompletionSignal(type="criteria_match", value=preds),
            ]
        )
        task.artifact_spec = ArtifactSpec(kind=ArtifactKind.OPS_RESULT)

        results = evaluate_artifact_signals(task, {"status": "ok"})
        assert [passed for _d, passed, _det in results] == [True, True]

    def test_evaluate_artifact_signals_ignores_filesystem_signals(self) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals
        from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec

        task = _make_task(signals=[CompletionSignal(type="path_exists", value="README.md")])
        task.artifact_spec = ArtifactSpec(kind=ArtifactKind.REPORT)
        assert evaluate_artifact_signals(task, "prose\n") == []


class TestArtifactSignalDefaultsFailClosed:
    """Issue #2968: a declared signal that no evaluator checked must not pass.

    ``evaluate_signal`` owns the per-type default. ``verify_task`` and
    ``_collect_signal_results`` both dispatch every signal through it, so the
    task-level paths cannot report a verdict the single-signal evaluator would
    not. Only ``evaluate_artifact_signals`` -- which has the produced artifact
    in scope -- can pass an artifact-mode signal.
    """

    # Types whose evaluation shells out (``test_passes``) or calls an LLM
    # (``llm_review``). Every other declared type is exercised below, so a
    # newly declared signal type joins the divergence check automatically.
    _SIDE_EFFECTING = frozenset({"test_passes", "llm_review"})

    @staticmethod
    def _declared_types() -> tuple[str, ...]:
        from typing import get_args, get_type_hints

        return get_args(get_type_hints(CompletionSignal)["type"])

    def _pure_types(self) -> list[str]:
        return [t for t in self._declared_types() if t not in self._SIDE_EFFECTING]

    def test_schema_valid_only_task_does_not_verify_as_passed(self, tmp_path: Path) -> None:
        """The headline case: the only signal is artifact-mode, nothing evaluated it."""
        task = _make_task(signals=[CompletionSignal(type="schema_valid", value='{"type": "object"}')])

        passed, failed = verify_task(task, tmp_path)

        assert passed is False
        assert len(failed) == 1
        assert "schema_valid" in failed[0]
        assert "evaluate_artifact_signals()" in failed[0]

    def test_artifact_types_are_declared_signal_types(self) -> None:
        from bernstein.core.quality.janitor import _ARTIFACT_SIGNAL_TYPES

        assert set(self._declared_types()).issuperset(_ARTIFACT_SIGNAL_TYPES)

    def test_verify_task_never_diverges_from_evaluate_signal(self, tmp_path: Path) -> None:
        """Both paths must agree per signal type -- not just for today's types."""
        for sig_type in self._pure_types():
            signal = CompletionSignal(type=sig_type, value="x")  # type: ignore[arg-type]
            single_passed, single_detail = evaluate_signal(signal, tmp_path)

            task_passed, failed = verify_task(_make_task(signals=[signal]), tmp_path)

            assert task_passed is single_passed, sig_type
            if single_passed:
                assert failed == [], sig_type
            else:
                assert failed == [f"{sig_type}: x ({single_detail})"], sig_type

    def test_collect_signal_results_agrees_with_verify_task(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import _ARTIFACT_SIGNAL_TYPES, _collect_signal_results

        for sig_type in sorted(_ARTIFACT_SIGNAL_TYPES):
            signal = CompletionSignal(type=sig_type, value="x")  # type: ignore[arg-type]
            task = _make_task(signals=[signal])

            verify_passed, _failed = verify_task(task, tmp_path)
            results = _collect_signal_results(task, tmp_path)

            assert verify_passed is False, sig_type
            assert len(results) == 1, sig_type
            desc, passed, detail = results[0]
            assert passed is verify_passed, sig_type
            assert desc == f"{sig_type}: x"
            assert detail == evaluate_signal(signal, tmp_path)[1]

    @pytest.mark.asyncio
    async def test_run_janitor_rejects_artifact_only_task(self, tmp_path: Path) -> None:
        task = _make_task(signals=[CompletionSignal(type="hash_stable", value="sha256:deadbeef")])

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        assert [desc for desc, ok, _ in results[0].signal_results if not ok] == ["hash_stable: sha256:deadbeef"]

    def test_filesystem_only_task_is_unchanged(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import _collect_signal_results

        (tmp_path / "out.txt").write_text("done")
        task = _make_task(signals=[CompletionSignal(type="path_exists", value="out.txt")])

        assert verify_task(task, tmp_path) == (True, [])
        assert _collect_signal_results(task, tmp_path) == [("path_exists: out.txt", True, "exists")]

    def test_passing_filesystem_signal_does_not_mask_artifact_signal(self, tmp_path: Path) -> None:
        (tmp_path / "out.txt").write_text("done")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="out.txt"),
                CompletionSignal(type="criteria_match", value="[]"),
            ]
        )

        passed, failed = verify_task(task, tmp_path)

        assert passed is False
        assert len(failed) == 1
        assert failed[0].startswith("criteria_match: []")


class TestFiguresGroundedSignal:
    """Issue #2888: the figures_grounded completion signal and its severity."""

    _HMAC = b"k" * 64

    def _seed(self, tmp_path: Path, body: str, declare_9_9: bool = False):
        """Record a source dataset into tmp_path/.sdd; return a report ReportBundle."""
        import json as _json

        from bernstein.core.lineage.artifact_record import record_artifact
        from bernstein.core.lineage.identity import AgentCard, generate_keypair
        from bernstein.core.lineage.recorder import LineageRecorder
        from bernstein.core.lineage.store import LineageStore
        from bernstein.core.tasks.artifacts import ArtifactKind
        from bernstein.core.tasks.figures import Figure, FigureAnchor, ReportBundle

        sdd = tmp_path / ".sdd"
        priv, pub = generate_keypair()
        card = AgentCard(agent_id="agent:analyst", kid="key-fg", public_key_pem=pub)
        rec = LineageRecorder(store=LineageStore(sdd / "lineage"), operator_hmac_key=self._HMAC)
        src = record_artifact(
            recorder=rec,
            sink_root=sdd / "artifacts",
            task_id="SRC",
            kind=ArtifactKind.DATASET,
            artifact=[{"users": 1234}],
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
        )
        card_dir = sdd / "agents" / card.agent_id
        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / "card.json").write_text(
            _json.dumps({"agent_id": card.agent_id, "kid": card.kid, "public_key_pem": card.public_key_pem}),
            encoding="utf-8",
        )
        figs = [Figure("1,234", "users", "migrated users", FigureAnchor("artifact", src.content_hash))]
        if declare_9_9:
            figs.append(Figure("9.9", "%", "cost ratio", FigureAnchor("artifact", src.content_hash)))
        return ReportBundle(body=body, figures=tuple(figs))

    def _task(self, value: str = ""):
        from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec

        task = _make_task(signals=[CompletionSignal(type="figures_grounded", value=value)])
        task.artifact_spec = ArtifactSpec(kind=ArtifactKind.REPORT)
        return task

    def test_grounded_report_passes(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals

        bundle = self._seed(tmp_path, "We migrated 1,234 users.\n")
        results = evaluate_artifact_signals(self._task(), bundle, lineage_root=tmp_path)
        assert len(results) == 1
        _desc, passed, detail = results[0]
        assert passed is True, detail
        assert "grounded" in detail

    def test_strict_unanchored_number_fails(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals

        bundle = self._seed(tmp_path, "We migrated 1,234 users at 9.9% cost.\n")
        results = evaluate_artifact_signals(self._task(value="strict"), bundle, lineage_root=tmp_path)
        _desc, passed, detail = results[0]
        assert passed is False
        assert "9.9%" in detail

    def test_warn_downgrades_failure_to_pass(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals

        bundle = self._seed(tmp_path, "We migrated 1,234 users at 9.9% cost.\n")
        results = evaluate_artifact_signals(self._task(value="warn"), bundle, lineage_root=tmp_path)
        _desc, passed, detail = results[0]
        assert passed is True
        assert detail.startswith("WARN:")
        assert "9.9%" in detail

    def test_default_severity_is_strict(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals

        bundle = self._seed(tmp_path, "We migrated 1,234 users at 9.9% cost.\n")
        results = evaluate_artifact_signals(self._task(value=""), bundle, lineage_root=tmp_path)
        _desc, passed, _detail = results[0]
        assert passed is False

    def test_non_bundle_artifact_reports_clearly(self, tmp_path: Path) -> None:
        from bernstein.core.quality.janitor import evaluate_artifact_signals

        results = evaluate_artifact_signals(self._task(value="strict"), "just prose", lineage_root=tmp_path)
        _desc, passed, detail = results[0]
        assert passed is False
        assert "report bundle" in detail
