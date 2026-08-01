"""``--plan-only`` must never reach the bootstrap entry point (gh-3255).

``plan_file`` is a positional argument, so ``bernstein run plan.yaml
--plan-only`` used to take the ``plan_file`` dispatch, which called
``bootstrap_from_goal`` and returned before the ``plan_only`` check was ever
consulted: a live agent, a worktree, a commit and the merge path, with the flag
silently dropped.

Every assertion here patches ``bernstein.core.bootstrap.bootstrap_from_goal``
rather than the ``run_bootstrap`` module attribute. ``_run_impl`` imports the
name inside the function body, so patching the attribute on ``run_bootstrap``
would pass without proving anything.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest

from bernstein.cli import run_bootstrap

PLAN_YAML = """\
name: "Checkout hardening"
description: "Two stages that must survive the preview intact."
cli: auto
stages:
  - name: "Infrastructure"
    steps:
      - title: "Add idempotency keys to the payment intent endpoint"
        role: backend
        scope: medium
        complexity: high
      - title: "Cover the retry path with contract tests"
        role: qa
        scope: small
        complexity: medium
  - name: "Hardening"
    steps:
      - title: "Audit the webhook signature check"
        role: security
        scope: small
        complexity: high
"""


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
def _run_impl_environment() -> Any:
    """Neutralise the environment-touching bootstrap steps of ``_run_impl``."""
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


@contextlib.contextmanager
def _no_execution() -> Any:
    """Patch every entry point that would start real work.

    ``bootstrap_from_goal`` is the single door to the task server, the
    watchdog, the spawner, the worktree and the merge path, so a preview that
    never calls it cannot have done any of them.
    """
    with (
        patch("bernstein.core.bootstrap.bootstrap_from_goal") as bootstrap_goal,
        patch("bernstein.core.bootstrap.bootstrap_from_seed") as bootstrap_seed,
        patch.object(run_bootstrap, "persist_server_port") as persist_port,
    ):
        yield bootstrap_goal, bootstrap_seed, persist_port


def _written_plan(workdir: Path) -> str:
    saved = sorted((workdir / ".sdd" / "runtime" / "plans").glob("plan-*.md"))
    assert saved, "--plan-only must save the rendered plan"
    return saved[-1].read_text(encoding="utf-8")


class TestPlanOnlyNeverExecutes:
    def test_positional_plan_file_with_plan_only_does_not_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported defect: ``bernstein run plan.yaml --plan-only``."""
        monkeypatch.chdir(tmp_path)
        plan = tmp_path / "plan.yaml"
        plan.write_text(PLAN_YAML, encoding="utf-8")

        with _run_impl_environment(), _no_execution() as (bootstrap_goal, bootstrap_seed, persist_port):
            run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True, goal=None, plan_file=plan))  # type: ignore[arg-type]

        bootstrap_goal.assert_not_called()
        bootstrap_seed.assert_not_called()
        persist_port.assert_not_called()

    def test_inline_goal_with_plan_only_does_not_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path that already worked stays working."""
        monkeypatch.chdir(tmp_path)

        with _run_impl_environment(), _no_execution() as (bootstrap_goal, bootstrap_seed, persist_port):
            run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True))  # type: ignore[arg-type]

        bootstrap_goal.assert_not_called()
        bootstrap_seed.assert_not_called()
        persist_port.assert_not_called()

    def test_from_plan_with_plan_only_does_not_bootstrap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--from-plan --plan-only`` previewed before this change; it still does."""
        monkeypatch.chdir(tmp_path)
        saved = tmp_path / "saved-plan.md"
        saved.write_text("# Plan\n\n**Goal:** Rotate the signing keys\n", encoding="utf-8")

        with _run_impl_environment(), _no_execution() as (bootstrap_goal, bootstrap_seed, _):
            run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True, goal=None, from_plan=saved))  # type: ignore[arg-type]

        bootstrap_goal.assert_not_called()
        bootstrap_seed.assert_not_called()
        assert "Rotate the signing keys" in _written_plan(tmp_path)


class TestPlanOnlyRendersTheLoadedPlan:
    def test_plan_file_preview_renders_the_loaded_tasks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not the synthetic single-manager plan built from a goal string."""
        monkeypatch.chdir(tmp_path)
        plan = tmp_path / "plan.yaml"
        plan.write_text(PLAN_YAML, encoding="utf-8")

        with _run_impl_environment(), _no_execution():
            run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True, goal=None, plan_file=plan))  # type: ignore[arg-type]

        rendered = _written_plan(tmp_path)
        assert "Add idempotency keys to the payment intent endpoint" in rendered
        assert "Cover the retry path with contract tests" in rendered
        assert "Audit the webhook signature check" in rendered

    def test_plan_file_preview_points_the_rerun_at_the_plan_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--from-plan`` reads only the ``**Goal:**`` line out of the saved
        markdown and re-decomposes from the plan name, dropping every task just
        previewed. The plan-file preview must not send the operator there."""
        monkeypatch.chdir(tmp_path)
        plan = tmp_path / "plan.yaml"
        plan.write_text(PLAN_YAML, encoding="utf-8")

        with _run_impl_environment(), _no_execution():
            run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True, goal=None, plan_file=plan))  # type: ignore[arg-type]

        out = capsys.readouterr().out
        assert "--from-plan" not in out
        assert "bernstein run" in out


class TestExecutingPathUnchanged:
    def test_plan_file_without_plan_only_still_bootstraps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hoisted check must not swallow a real ``bernstein run plan.yaml``."""
        monkeypatch.chdir(tmp_path)
        plan = tmp_path / "plan.yaml"
        plan.write_text(PLAN_YAML, encoding="utf-8")

        with (
            _run_impl_environment(),
            _no_execution() as (bootstrap_goal, _, _persist),
            patch.object(run_bootstrap, "_abort_if_default_branch_merge_target"),
            patch.object(run_bootstrap, "_estimate_run_preview"),
            patch.object(run_bootstrap, "_emit_preflight_runtime_warnings"),
            patch.object(run_bootstrap, "_finalize_run_output"),
        ):
            run_bootstrap._run_impl(**_run_impl_kwargs(goal=None, plan_file=plan))  # type: ignore[arg-type]

        bootstrap_goal.assert_called_once()

    def test_worker_with_plan_file_still_refused_under_plan_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--worker`` bypasses manager decomposition, so it has nothing to
        preview; the refusal must fire ahead of the preview, not behind it."""
        monkeypatch.chdir(tmp_path)
        plan = tmp_path / "plan.yaml"
        plan.write_text(PLAN_YAML, encoding="utf-8")

        with _run_impl_environment(), _no_execution() as (bootstrap_goal, _, _persist):
            with pytest.raises(click.UsageError):
                run_bootstrap._run_impl(  # type: ignore[arg-type]
                    **_run_impl_kwargs(plan_only=True, goal=None, plan_file=plan, worker_role="backend")
                )

        bootstrap_goal.assert_not_called()

    def test_unreadable_plan_file_exits_one_without_bootstrapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        plan = tmp_path / "plan.yaml"
        plan.write_text("stages: [[[", encoding="utf-8")

        with _run_impl_environment(), _no_execution() as (bootstrap_goal, _, _persist):
            with pytest.raises(SystemExit):
                run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True, goal=None, plan_file=plan))  # type: ignore[arg-type]

        bootstrap_goal.assert_not_called()


class TestSavedPlanEncoding:
    def test_saved_plan_is_written_as_utf8(self, tmp_path: Path) -> None:
        """The render always emits status glyphs, so the platform default
        encoding raises UnicodeEncodeError on a cp1252 locale."""
        saved = run_bootstrap._save_plan_markdown("# Plan\n\n- ✓ done\n- ⚠ blocked\n- ⚡ fast\n", tmp_path)
        assert saved.read_bytes().decode("utf-8").count("✓") == 1


def test_plan_only_reaches_no_default_branch_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview does no merging, so it must not abort on a default-branch
    merge target either -- including when the plan came from a file."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(PLAN_YAML, encoding="utf-8")
    guard = MagicMock()

    with (
        _run_impl_environment(),
        _no_execution(),
        patch.object(run_bootstrap, "_abort_if_default_branch_merge_target", guard),
    ):
        run_bootstrap._run_impl(**_run_impl_kwargs(plan_only=True, goal=None, plan_file=plan))  # type: ignore[arg-type]

    guard.assert_not_called()
