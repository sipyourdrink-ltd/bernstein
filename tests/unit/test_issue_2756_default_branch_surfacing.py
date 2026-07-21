"""Surfacing for default-branch merge refusals (gh-2756).

Running on the repository default branch used to let every agent do its work,
have the spawner merge guard silently discard the merged result, and still
end with a clean "Tasks completed 0/0" / HEALTHY summary. Three surfaces fix
that:

A. CLI preflight aborts before any agent spawns when the run would merge back
   onto the default branch without the explicit override - but only for run
   modes that actually merge back (--dry-run and --plan-only stay untouched).
B. A merge refusal that still happens mid-run is printed in the CLI run
   summary instead of staying buried in the spawner log.
C. Run health treats discarded work as a hard UNHEALTHY rule with the reason.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.metrics import MetricsCollector
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

import bernstein.cli.run_bootstrap as run_bootstrap
from bernstein.cli.run_preflight import (
    _abort_if_default_branch_merge_target,
    _show_run_summary,
    _surface_merge_refusals,
)
from bernstein.cli.ui import make_console
from bernstein.core.quality.retrospective import (
    compute_run_health,
    generate_retrospective,
    read_merge_refusals,
)

if TYPE_CHECKING:
    from pathlib import Path

OVERRIDE_ENV = "BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(*, id: str = "T-001", status: str = "done") -> Task:
    return Task(
        id=id,
        title="Do something",
        description="desc",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity("medium"),
        status=TaskStatus(status),
        task_type=TaskType.STANDARD,
    )


def _write_refusal(
    runtime_dir: Path,
    *,
    branch: str = "main",
    ts: float | None = None,
    session_id: str = "manager-1",
) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id,
        "branch": branch,
        "reason": "target-is-default-branch",
        "ts": time.time() if ts is None else ts,
    }
    with (runtime_dir / "refused_merges.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# A. Preflight guard behaviour
# ---------------------------------------------------------------------------


class TestPreflightDefaultBranchGuard:
    def test_aborts_on_default_branch_without_override(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(OVERRIDE_ENV, raising=False)
        with (
            patch("bernstein.core.git_ops.current_branch", return_value="main"),
            patch(
                "bernstein.core.git_ops.protected_default_branches",
                return_value=frozenset({"main"}),
            ),
        ):
            with pytest.raises(SystemExit) as excinfo:
                _abort_if_default_branch_merge_target(tmp_path)

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        # The error must name both remedies: the override env var and the
        # checkout-a-working-branch alternative.
        assert OVERRIDE_ENV in out
        assert "checkout" in out
        assert "main" in out

    def test_aborts_on_ambiguous_default_when_either_candidate_checked_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mirror the merge guard's fail-closed ambiguity handling."""
        monkeypatch.delenv(OVERRIDE_ENV, raising=False)
        with (
            patch("bernstein.core.git_ops.current_branch", return_value="master"),
            patch(
                "bernstein.core.git_ops.protected_default_branches",
                return_value=frozenset({"main", "master"}),
            ),
        ):
            with pytest.raises(SystemExit):
                _abort_if_default_branch_merge_target(tmp_path)

    def test_no_abort_on_feature_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERRIDE_ENV, raising=False)
        with (
            patch("bernstein.core.git_ops.current_branch", return_value="feat/x"),
            patch(
                "bernstein.core.git_ops.protected_default_branches",
                return_value=frozenset({"main"}),
            ),
        ):
            _abort_if_default_branch_merge_target(tmp_path)

    def test_no_abort_with_override_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OVERRIDE_ENV, "1")
        with (
            patch("bernstein.core.git_ops.current_branch", return_value="main"),
            patch(
                "bernstein.core.git_ops.protected_default_branches",
                return_value=frozenset({"main"}),
            ),
        ):
            _abort_if_default_branch_merge_target(tmp_path)

    def test_no_abort_on_detached_head_or_missing_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """current_branch is None on detached HEAD and outside a repo; the
        merge guard skips both, so preflight must skip them too."""
        monkeypatch.delenv(OVERRIDE_ENV, raising=False)
        with patch("bernstein.core.git_ops.current_branch", return_value=None):
            _abort_if_default_branch_merge_target(tmp_path)


# ---------------------------------------------------------------------------
# A (wiring). Merge-back modes hit the guard; no-merge modes never do.
# ---------------------------------------------------------------------------


def _run_impl_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "plan_file": None,
        "goal": "Ship it",
        "seed_file": None,
        "port": 8052,
        "cells": 1,
        "remote": False,
        "cli": None,
        "model": None,
        "workflow": None,
        "routing": None,
        "compliance": None,
        "container": False,
        "container_image": None,
        "two_phase_sandbox": False,
        "worker_role": None,
        "plan_only": False,
        "from_plan": None,
        "auto_approve": True,
        "quiet": True,
        "skip_gate": (),
        "skip_gate_reason": None,
        "audit": False,
        "sandbox": None,
        "allow_paid": False,
        "ab_test": False,
        "dry_run": False,
        "idle": False,
        "cprofile": False,
        "run_profile": None,
        "allow_network": (),
        "permission_profile": None,
        "task_filter": None,
        "auto_pr": False,
        "activity_log_path": None,
        "max_cost_usd": None,
        "budget_spec": None,
        "hard_budget_spec": None,
        "budget_cap": None,
        "retry_budget_spec": None,
        "criterion_profile": None,
        "max_blast_radius": None,
        "attach": (),
        "refresh_cache": False,
    }
    kwargs.update(overrides)
    return kwargs


@contextlib.contextmanager
def _run_impl_environment() -> object:
    """Neutralise the environment-touching bootstrap steps of _run_impl."""
    with contextlib.ExitStack() as stack:
        for patcher in (
            patch.object(run_bootstrap, "print_startup_banner"),
            patch.object(run_bootstrap, "_propagate_env_flags"),
            patch.object(run_bootstrap, "_sovereign_config_snapshot", return_value=None),
            patch.object(run_bootstrap, "_install_profile_network_policy"),
            patch.object(run_bootstrap, "_activate_sovereign_profile"),
            patch.object(run_bootstrap, "_configure_quality_gate_bypass"),
        ):
            stack.enter_context(patcher)
        yield


class TestRunImplGuardWiring:
    def test_dry_run_never_invokes_default_branch_guard(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        guard = MagicMock()
        with (
            _run_impl_environment(),
            patch.object(run_bootstrap, "_show_dry_run_plan") as show_plan,
            patch.object(run_bootstrap, "_abort_if_default_branch_merge_target", guard),
        ):
            run_bootstrap._run_impl(**_run_impl_kwargs(dry_run=True))  # type: ignore[arg-type]

        show_plan.assert_called_once()
        guard.assert_not_called()

    def test_plan_only_never_invokes_default_branch_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        guard = MagicMock()
        with (
            _run_impl_environment(),
            patch.object(run_bootstrap, "_abort_if_default_branch_merge_target", guard),
        ):
            run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True))  # type: ignore[arg-type]

        guard.assert_not_called()

    def test_goal_mode_aborts_before_estimate_and_spawn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        guard = MagicMock(side_effect=SystemExit(1))
        with (
            _run_impl_environment(),
            patch.object(run_bootstrap, "_abort_if_default_branch_merge_target", guard),
            patch.object(run_bootstrap, "_estimate_run_preview") as estimate,
            patch("bernstein.core.bootstrap.bootstrap_from_goal") as bootstrap_goal,
        ):
            with pytest.raises(SystemExit):
                run_bootstrap._run_impl(**_run_impl_kwargs())  # type: ignore[arg-type]

        guard.assert_called_once()
        estimate.assert_not_called()
        bootstrap_goal.assert_not_called()


# ---------------------------------------------------------------------------
# B. CLI run summary surfaces refusals
# ---------------------------------------------------------------------------


class TestCliSummarySurfacesRefusals:
    def test_fresh_refusal_prints_warning_with_both_remedies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_refusal(tmp_path / ".sdd" / "runtime", branch="main")
        con = make_console(no_color=True)

        _surface_merge_refusals(tmp_path, since_ts=0.0, console=con)

        out = capsys.readouterr().out
        assert "refused" in out
        assert "main" in out
        assert OVERRIDE_ENV in out
        assert "checkout" in out

    def test_stale_refusals_from_prior_runs_stay_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_refusal(tmp_path / ".sdd" / "runtime", ts=time.time() - 3600)
        con = make_console(no_color=True)

        _surface_merge_refusals(tmp_path, since_ts=time.time() - 60, console=con)

        assert capsys.readouterr().out == ""

    def test_no_journal_stays_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        con = make_console(no_color=True)

        _surface_merge_refusals(tmp_path, since_ts=0.0, console=con)

        assert capsys.readouterr().out == ""

    def test_show_run_summary_surfaces_refusals_even_when_server_gone(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_refusal(tmp_path / ".sdd" / "runtime", branch="main")
        with patch("bernstein.cli.helpers.server_get", return_value=None):
            _show_run_summary()

        out = capsys.readouterr().out
        assert "refused" in out
        assert OVERRIDE_ENV in out


# ---------------------------------------------------------------------------
# C. Run health downgrade
# ---------------------------------------------------------------------------


class TestRunHealthWithRefusedMerges:
    def test_zero_task_run_with_refused_merge_is_unhealthy(self) -> None:
        healthy, counts = compute_run_health([], n_refused_merges=1)
        assert healthy is False
        assert counts == {}

    def test_successful_tasks_with_refused_merge_still_unhealthy(self) -> None:
        tasks = [_make_task(id=f"T-{i}", status="done") for i in range(3)]
        healthy, _counts = compute_run_health(tasks, n_refused_merges=2)
        assert healthy is False

    def test_no_refusals_keeps_existing_verdict(self) -> None:
        tasks = [_make_task(id=f"T-{i}", status="done") for i in range(3)]
        healthy, _counts = compute_run_health(tasks, n_refused_merges=0)
        assert healthy is True

    def test_empty_run_without_refusals_stays_vacuously_healthy(self) -> None:
        healthy, counts = compute_run_health([])
        assert healthy is True
        assert counts == {}


class TestReadMergeRefusals:
    def test_reads_entries_and_filters_by_since_ts(self, tmp_path: Path) -> None:
        now = time.time()
        _write_refusal(tmp_path, branch="main", ts=now - 3600, session_id="old")
        _write_refusal(tmp_path, branch="main", ts=now, session_id="fresh")

        refusals = read_merge_refusals(tmp_path, since_ts=now - 60)

        assert len(refusals) == 1
        assert refusals[0].session_id == "fresh"
        assert refusals[0].branch == "main"
        assert refusals[0].reason == "target-is-default-branch"

    def test_missing_journal_returns_empty(self, tmp_path: Path) -> None:
        assert read_merge_refusals(tmp_path) == []

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        _write_refusal(tmp_path, branch="main", ts=time.time())
        path = tmp_path / "refused_merges.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write('["not", "a", "dict"]\n')
            handle.write('{"session_id": "no-ts", "branch": "main", "reason": "x"}\n')

        refusals = read_merge_refusals(tmp_path)

        assert len(refusals) == 1


class TestRetrospectiveReflectsRefusedMerges:
    def test_fresh_refusal_downgrades_health_and_names_reason(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        run_start = time.time() - 60
        _write_refusal(runtime_dir, branch="main")
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")

        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=runtime_dir,
            run_start_ts=run_start,
        )

        content = (runtime_dir / "retrospective.md").read_text()
        assert "**Verdict:** UNHEALTHY" in content
        assert "Merge refused" in content
        assert "default branch" in content

    def test_stale_refusals_from_prior_runs_keep_health(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        run_start = time.time() - 60
        _write_refusal(runtime_dir, branch="main", ts=run_start - 3600)
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")

        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=runtime_dir,
            run_start_ts=run_start,
        )

        content = (runtime_dir / "retrospective.md").read_text()
        assert "**Verdict:** HEALTHY" in content
