"""Unit tests for the bounded test-authoring follow-up (issue #4462).

Covers, per the issue's acceptance criteria:

* trigger - a src-without-tests diff schedules exactly one follow-up whose
  goal carries the file list;
* non-trigger - a diff that already includes tests never schedules one;
* no-loop - once a follow-up has been scheduled for a run, a later
  evaluation (including one fed the follow-up's own diff) never schedules a
  second one;
* switch-off - the config/env switch disables the behaviour entirely.

``evaluate_test_followup`` is pure, so these are fed synthetic diffs
directly rather than driven through a live server - the "stubbed
ledger/spawner" style the issue's test plan asks for. The git-touching
helpers (``resolve_run_branch``, ``diff_name_only``) get their own class
against a real temporary repository, since faking git here would just
re-implement it. The end-to-end path - a scripted adapter proving the
follow-up task actually reaches ``POST /tasks`` with the file list in its
goal - lives in ``tests/integration/test_test_followup_e2e.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.orchestration.test_followup import (
    ENV_TEST_FOLLOWUP,
    DiffClassification,
    build_followup_goal,
    classify_diff,
    diff_name_only,
    evaluate_test_followup,
    resolve_run_branch,
    resolve_test_followup_enabled,
)
from bernstein.core.tasks.models import Task


def _task(task_id: str, *, assigned_agent: str | None, completed_at: float | None) -> Task:
    return Task(
        id=task_id,
        title=task_id,
        description="d",
        role="backend",
        assigned_agent=assigned_agent,
        completed_at=completed_at,
    )


class TestClassifyDiff:
    def test_partitions_src_and_tests(self) -> None:
        result = classify_diff(["src/a.py", "src/sub/b.py", "tests/test_a.py"])
        assert result == DiffClassification(
            src_files=("src/a.py", "src/sub/b.py"),
            test_files=("tests/test_a.py",),
        )

    def test_unrelated_paths_are_dropped(self) -> None:
        result = classify_diff(["docs/readme.md", "bernstein.yaml", "README.md"])
        assert result.src_files == ()
        assert result.test_files == ()
        assert result.needs_followup is False

    def test_needs_followup_true_only_for_src_without_tests(self) -> None:
        assert classify_diff(["src/a.py"]).needs_followup is True
        assert classify_diff(["src/a.py", "tests/t.py"]).needs_followup is False
        assert classify_diff(["tests/t.py"]).needs_followup is False
        assert classify_diff([]).needs_followup is False

    def test_normalises_and_dedupes_paths(self) -> None:
        result = classify_diff(["./src/a.py", "src\\a.py", "src/a.py"])
        assert result.src_files == ("src/a.py",)

    def test_bare_src_and_tests_directory_entries_count(self) -> None:
        # A rename/delete can surface the bare directory itself.
        assert classify_diff(["src"]).src_files == ("src",)
        assert classify_diff(["tests"]).test_files == ("tests",)


class TestEvaluateTestFollowup:
    """Pins the four required behaviours directly against the pure criterion."""

    def test_trigger_on_src_without_tests(self) -> None:
        decision = evaluate_test_followup(
            enabled=True,
            already_scheduled=False,
            changed_files=["src/foo.py", "src/bar.py"],
        )
        assert decision.should_schedule is True
        assert decision.reason == "src_without_tests"
        assert decision.src_files == ("src/foo.py", "src/bar.py")

    def test_non_trigger_when_diff_already_has_tests(self) -> None:
        decision = evaluate_test_followup(
            enabled=True,
            already_scheduled=False,
            changed_files=["src/foo.py", "tests/test_foo.py"],
        )
        assert decision.should_schedule is False
        assert decision.reason == "tests_present"

    def test_non_trigger_when_no_src_changes(self) -> None:
        decision = evaluate_test_followup(
            enabled=True,
            already_scheduled=False,
            changed_files=["docs/readme.md"],
        )
        assert decision.should_schedule is False
        assert decision.reason == "no_src_changes"

    def test_no_loop_once_already_scheduled(self) -> None:
        # Same src-without-tests diff that triggered above, fed again with
        # already_scheduled=True (as it would be after the follow-up's own
        # completion re-triggers quiescence) - must never fire twice.
        decision = evaluate_test_followup(
            enabled=True,
            already_scheduled=True,
            changed_files=["src/foo.py"],
        )
        assert decision.should_schedule is False
        assert decision.reason == "already_scheduled"

    def test_switch_off_disables_even_a_triggering_diff(self) -> None:
        decision = evaluate_test_followup(
            enabled=False,
            already_scheduled=False,
            changed_files=["src/foo.py"],
        )
        assert decision.should_schedule is False
        assert decision.reason == "disabled"

    def test_disabled_takes_precedence_over_already_scheduled_reason(self) -> None:
        # Ordering check: disabled must not report "already_scheduled".
        decision = evaluate_test_followup(enabled=False, already_scheduled=True, changed_files=["src/foo.py"])
        assert decision.reason == "disabled"


class TestBuildFollowupGoal:
    def test_carries_the_file_list(self) -> None:
        goal = build_followup_goal(("src/foo.py", "src/bar.py"))
        assert "src/foo.py" in goal
        assert "src/bar.py" in goal

    def test_instructs_tests_only_scope(self) -> None:
        goal = build_followup_goal(("src/foo.py",))
        assert "tests/" in goal
        assert "do not modify src/" in goal

    def test_deterministic_for_the_same_input(self) -> None:
        assert build_followup_goal(("src/a.py", "src/b.py")) == build_followup_goal(("src/a.py", "src/b.py"))


class TestResolveTestFollowupEnabled:
    def test_env_unset_falls_back_to_config_value(self) -> None:
        assert resolve_test_followup_enabled(True, env={}) is True
        assert resolve_test_followup_enabled(False, env={}) is False

    def test_env_truthy_overrides_config_false(self) -> None:
        for word in ("1", "true", "yes", "on", "enabled"):
            assert resolve_test_followup_enabled(False, env={ENV_TEST_FOLLOWUP: word}) is True

    def test_env_falsy_overrides_config_true(self) -> None:
        for word in ("0", "false", "no", "off", "disabled"):
            assert resolve_test_followup_enabled(True, env={ENV_TEST_FOLLOWUP: word}) is False

    def test_env_unrecognised_falls_back_to_config_value(self) -> None:
        assert resolve_test_followup_enabled(True, env={ENV_TEST_FOLLOWUP: "maybe"}) is True
        assert resolve_test_followup_enabled(False, env={ENV_TEST_FOLLOWUP: "maybe"}) is False

    def test_reads_real_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_TEST_FOLLOWUP, "0")
        assert resolve_test_followup_enabled(True) is False
        monkeypatch.delenv(ENV_TEST_FOLLOWUP, raising=False)
        assert resolve_test_followup_enabled(True) is True


# ---------------------------------------------------------------------------
# Git integration helpers - real temporary repository, no mocking git itself.
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_branches(tmp_path: Path) -> Path:
    """A repo on ``main`` with a src+test change on ``agent/with-tests`` and
    a src-only change on ``agent/no-tests``, both branched from the same
    root commit.
    """
    _git(["init", "-q", "-b", "main", "."], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "foo.py").write_text("print(1)\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    _git(["checkout", "-q", "-b", "agent/no-tests"], tmp_path)
    (tmp_path / "src" / "foo.py").write_text("print(2)\n")
    (tmp_path / "src" / "bar.py").write_text("print(3)\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "src only"], tmp_path)
    _git(["checkout", "-q", "main"], tmp_path)

    _git(["checkout", "-q", "-b", "agent/with-tests"], tmp_path)
    (tmp_path / "src" / "foo.py").write_text("print(4)\n")
    (tmp_path / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "src and tests"], tmp_path)
    _git(["checkout", "-q", "main"], tmp_path)

    return tmp_path


class TestResolveRunBranch:
    def test_picks_the_most_recently_completed_tasks_branch(self, repo_with_branches: Path) -> None:
        older = _task("t-old", assigned_agent="with-tests", completed_at=100.0)
        newer = _task("t-new", assigned_agent="no-tests", completed_at=200.0)
        assert resolve_run_branch(repo_with_branches, [older, newer]) == "agent/no-tests"

    def test_falls_back_past_a_task_whose_branch_no_longer_exists(self, repo_with_branches: Path) -> None:
        gone = _task("t-gone", assigned_agent="already-merged-and-deleted", completed_at=999.0)
        earlier = _task("t-earlier", assigned_agent="no-tests", completed_at=1.0)
        assert resolve_run_branch(repo_with_branches, [gone, earlier]) == "agent/no-tests"

    def test_none_when_no_task_has_a_resolvable_branch(self, repo_with_branches: Path) -> None:
        gone = _task("t-gone", assigned_agent="nope", completed_at=1.0)
        assert resolve_run_branch(repo_with_branches, [gone]) is None

    def test_none_for_empty_task_list(self, repo_with_branches: Path) -> None:
        assert resolve_run_branch(repo_with_branches, []) is None

    def test_skips_tasks_with_no_assigned_agent(self, repo_with_branches: Path) -> None:
        unassigned = _task("t-unassigned", assigned_agent=None, completed_at=500.0)
        assert resolve_run_branch(repo_with_branches, [unassigned]) is None


class TestDiffNameOnly:
    def test_reports_changed_files_on_a_src_only_branch(self, repo_with_branches: Path) -> None:
        changed = diff_name_only(repo_with_branches, "main", "agent/no-tests")
        assert set(changed) == {"src/bar.py", "src/foo.py"}

    def test_reports_changed_files_on_a_branch_with_tests(self, repo_with_branches: Path) -> None:
        changed = diff_name_only(repo_with_branches, "main", "agent/with-tests")
        assert set(changed) == {"src/foo.py", "tests/test_foo.py"}

    def test_empty_on_git_failure_rather_than_raising(self, repo_with_branches: Path) -> None:
        assert diff_name_only(repo_with_branches, "main", "branch/does/not/exist") == ()
