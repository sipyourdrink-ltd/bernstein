"""Regression tests: fail fast before cost estimate or plan approval when no CLI adapter exists."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bernstein.cli import run_bootstrap


@contextlib.contextmanager
def _run_env(*, discovery_agents: list[object] | None = None):
    discovery = SimpleNamespace(agents=discovery_agents or [], warnings=[])
    with (
        patch.object(run_bootstrap, "print_startup_banner"),
        patch.object(run_bootstrap, "_propagate_env_flags"),
        patch.object(run_bootstrap, "_sovereign_config_snapshot", return_value=None),
        patch.object(run_bootstrap, "_install_profile_network_policy"),
        patch.object(run_bootstrap, "_activate_sovereign_profile"),
        patch.object(run_bootstrap, "_configure_quality_gate_bypass"),
        patch.object(run_bootstrap, "_abort_if_default_branch_merge_target"),
        patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery),
    ):
        yield


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "plan_file": None,
        "goal": "Add hello() to src/greeting.py",
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
        "auto_approve": False,
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
    base.update(overrides)
    return base


@pytest.mark.parametrize("auto_approve", [False, True])
def test_inline_goal_fails_fast_before_estimate_or_confirm_when_no_cli_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], auto_approve: bool
) -> None:
    """``bernstein -g`` aborts before cost estimation, planning, or auto-approval."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    estimate = MagicMock()
    confirm = MagicMock()
    bootstrap_goal = MagicMock()

    with (
        _run_env(discovery_agents=[]),
        patch.object(run_bootstrap, "_estimate_run_preview", estimate),
        patch.object(run_bootstrap, "_confirm_run", confirm),
        patch("bernstein.core.bootstrap.bootstrap_from_goal", bootstrap_goal),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(auto_approve=auto_approve))  # type: ignore[arg-type]

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "No CLI agents found" in out
    assert "bernstein demo" in out
    estimate.assert_not_called()
    confirm.assert_not_called()
    bootstrap_goal.assert_not_called()


def test_seed_auto_cli_fails_fast_before_estimate_when_no_cli_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``bernstein.yaml`` with ``cli: auto`` fails before previewing cost."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    (tmp_path / "bernstein.yaml").write_text('goal: "Ship feature"\ncli: auto\n', encoding="utf-8")
    estimate = MagicMock()

    with (
        _run_env(discovery_agents=[]),
        patch.object(run_bootstrap, "_estimate_run_preview", estimate),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(goal=None, auto_approve=True))  # type: ignore[arg-type]

    assert exc.value.code == 1
    estimate.assert_not_called()


def test_explicit_missing_cli_fails_fast_before_estimate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--cli codex`` when codex is absent from PATH aborts before preview/confirm."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    estimate = MagicMock()
    confirm = MagicMock()

    with (
        _run_env(discovery_agents=[]),
        patch("bernstein.core.orchestration.preflight.shutil.which", return_value=None),
        patch.object(run_bootstrap, "_estimate_run_preview", estimate),
        patch.object(run_bootstrap, "_confirm_run", confirm),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(cli="codex"))  # type: ignore[arg-type]

    assert exc.value.code == 1
    estimate.assert_not_called()
    confirm.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"dry_run": True},
        {"plan_only": True},
        {"idle": True, "auto_approve": True},
    ],
)
def test_exempt_modes_do_not_require_local_cli_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    """--dry-run, --plan-only, and --idle skip local CLI binary checks."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    monkeypatch.setattr(run_bootstrap, "_finalize_run_output", lambda **_: None)
    monkeypatch.setattr(run_bootstrap, "persist_server_port", lambda *_a, **_k: None)

    with (
        _run_env(discovery_agents=[]),
        patch.object(run_bootstrap, "_show_dry_run_plan"),
        patch.object(run_bootstrap, "_estimate_run_preview"),
        patch.object(run_bootstrap, "_emit_preflight_runtime_warnings"),
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
    ):
        run_bootstrap._run_impl(**_kwargs(**overrides))  # type: ignore[arg-type]


def test_remote_flag_still_checks_local_cli_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--remote binds the server to 0.0.0.0 but still starts the local spawner."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    estimate = MagicMock()
    confirm = MagicMock()

    with (
        _run_env(discovery_agents=[]),
        patch.object(run_bootstrap, "_estimate_run_preview", estimate),
        patch.object(run_bootstrap, "_confirm_run", confirm),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(remote=True))  # type: ignore[arg-type]

    assert exc.value.code == 1
    estimate.assert_not_called()
    confirm.assert_not_called()


def test_auto_cli_validates_env_pinned_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--cli auto with BERNSTEIN_ADAPTER=codex checks codex even when another agent is discovered."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_ADAPTER", "codex")
    estimate = MagicMock()
    confirm = MagicMock()
    other_agent = SimpleNamespace(name="claude", available_models=["sonnet"], logged_in=True)

    with (
        _run_env(discovery_agents=[other_agent]),
        patch("bernstein.core.orchestration.preflight.shutil.which", return_value=None),
        patch.object(run_bootstrap, "_estimate_run_preview", estimate),
        patch.object(run_bootstrap, "_confirm_run", confirm),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(cli="auto"))  # type: ignore[arg-type]

    assert exc.value.code == 1
    estimate.assert_not_called()
    confirm.assert_not_called()
