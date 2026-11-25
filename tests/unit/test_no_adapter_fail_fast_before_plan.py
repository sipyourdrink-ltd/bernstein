"""Regression tests: fail fast before cost estimate or plan approval when no CLI adapter exists."""

from __future__ import annotations

import contextlib
import inspect
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
    base: dict[str, object] = {}
    for name, param in inspect.signature(run_bootstrap._run_impl).parameters.items():
        if param.default is not inspect.Parameter.empty:
            base[name] = param.default
        elif "bool" in str(param.annotation):
            base[name] = False
        elif "tuple" in str(param.annotation):
            base[name] = ()
        else:
            base[name] = None
    return {**base, "goal": "Add hello() to src/greeting.py", "port": 8052, "cells": 1, "quiet": True, **overrides}


def _assert_aborts_before_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    discovery_agents: list[object] | None = None,
    which_fn: object | None = None,
    **overrides: object,
) -> None:
    monkeypatch.chdir(tmp_path)
    estimate, confirm, bootstrap_goal = MagicMock(), MagicMock(), MagicMock()
    which_patch = (
        patch("bernstein.core.orchestration.preflight.shutil.which", side_effect=which_fn)
        if callable(which_fn)
        else patch("bernstein.core.orchestration.preflight.shutil.which", return_value=None)
    )
    with (
        _run_env(discovery_agents=discovery_agents),
        which_patch,
        patch.object(run_bootstrap, "_estimate_run_preview", estimate),
        patch.object(run_bootstrap, "_confirm_run", confirm),
        patch("bernstein.core.bootstrap.bootstrap_from_goal", bootstrap_goal),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(**overrides))  # type: ignore[arg-type]
    assert exc.value.code == 1
    estimate.assert_not_called()
    confirm.assert_not_called()
    bootstrap_goal.assert_not_called()


@pytest.mark.parametrize("auto_approve", [False, True])
def test_inline_goal_fails_fast_before_estimate_or_confirm_when_no_cli_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], auto_approve: bool
) -> None:
    """``bernstein -g`` aborts before cost estimation, planning, or auto-approval."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    _assert_aborts_before_preview(tmp_path, monkeypatch, auto_approve=auto_approve)
    out = capsys.readouterr().out
    assert "No CLI agents found" in out and "bernstein demo" in out


def test_seed_auto_cli_fails_fast_before_estimate_when_no_cli_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``bernstein.yaml`` with ``cli: auto`` fails before previewing cost."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    (tmp_path / "bernstein.yaml").write_text('goal: "Ship feature"\ncli: auto\n', encoding="utf-8")
    _assert_aborts_before_preview(tmp_path, monkeypatch, goal=None, auto_approve=True)


def test_explicit_missing_cli_fails_fast_before_estimate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--cli codex`` when codex is absent from PATH aborts before preview/confirm."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    _assert_aborts_before_preview(tmp_path, monkeypatch, cli="codex")


@pytest.mark.parametrize("overrides", [{"dry_run": True}, {"plan_only": True}, {"idle": True, "auto_approve": True}])
def test_exempt_modes_do_not_require_local_cli_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    """--dry-run, --plan-only, and --idle skip local CLI binary checks."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    monkeypatch.delenv("BERNSTEIN_MOCK_IDLE", raising=False)
    monkeypatch.setattr(run_bootstrap, "_finalize_run_output", lambda **_: None)
    monkeypatch.setattr(run_bootstrap, "persist_server_port", lambda *_a, **_k: None)
    with (
        _run_env(discovery_agents=[]),
        patch.dict("os.environ", {}, clear=False),
        patch.object(run_bootstrap, "_show_dry_run_plan"),
        patch.object(run_bootstrap, "_estimate_run_preview"),
        patch.object(run_bootstrap, "_emit_preflight_runtime_warnings"),
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
    ):
        run_bootstrap._run_impl(**_kwargs(**overrides))  # type: ignore[arg-type]


def test_remote_flag_still_checks_local_cli_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--remote binds the server to 0.0.0.0 but still starts the local spawner."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    _assert_aborts_before_preview(tmp_path, monkeypatch, remote=True)


def test_auto_cli_validates_env_pinned_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--cli auto with BERNSTEIN_ADAPTER=codex checks codex even when another agent is discovered."""
    monkeypatch.setenv("BERNSTEIN_ADAPTER", "codex")
    other = SimpleNamespace(name="claude", available_models=["sonnet"], logged_in=True)
    _assert_aborts_before_preview(tmp_path, monkeypatch, discovery_agents=[other], cli="auto")


def test_pinned_alias_adapter_resolves_to_actual_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BERNSTEIN_ADAPTER=q_dev resolves to the ``q`` binary rather than looking for ``q_dev``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_ADAPTER", "q_dev")
    seen: list[str] = []

    def fake_which(name: str, path: str | None = None) -> str | None:
        seen.append(name)
        return "/usr/local/bin/q" if name == "q" else None

    with (
        _run_env(discovery_agents=[]),
        patch("bernstein.core.orchestration.preflight.shutil.which", side_effect=fake_which),
        patch.object(run_bootstrap, "_estimate_run_preview", side_effect=SystemExit(0)),
        pytest.raises(SystemExit) as exc,
    ):
        run_bootstrap._run_impl(**_kwargs(cli="auto"))  # type: ignore[arg-type]
    assert exc.value.code == 0 and seen == ["q"]


def test_guard_matches_bootstrap_from_seed_with_env_pin_over_seed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With bernstein.yaml cli=claude and BERNSTEIN_ADAPTER=codex, both guard and bootstrap resolve codex."""
    from bernstein.core.seed import parse_seed

    from bernstein.cli.run_preflight import _abort_if_no_cli_adapter
    from bernstein.core.orchestration import bootstrap as bootstrap_mod

    seed_path = tmp_path / "bernstein.yaml"
    seed_path.write_text('goal: "Ship feature"\ncli: claude\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_ADAPTER", "codex")
    checked, preflight_seen = [], []

    class StopAfterPreflight(RuntimeError):
        pass

    def fake_which(name: str, path: str | None = None) -> str | None:
        checked.append(name)
        return "/usr/local/bin/codex" if name == "codex" else None

    def fake_preflight(cli_arg: str, port: int, **_kw: object) -> None:
        preflight_seen.append(cli_arg)
        raise StopAfterPreflight

    with (
        patch("bernstein.core.orchestration.preflight.shutil.which", side_effect=fake_which),
        patch("bernstein.core.orchestration.bootstrap._acquire_pid_lock"),
        patch("bernstein.core.orchestration.bootstrap._run_git_hygiene"),
        patch("bernstein.core.orchestration.bootstrap.preflight_checks", side_effect=fake_preflight),
    ):
        _abort_if_no_cli_adapter(cli=None, seed=parse_seed(seed_path))
        with pytest.raises(StopAfterPreflight):
            bootstrap_mod.bootstrap_from_seed(seed_path, tmp_path, cli=None)
    assert checked == ["codex"] and preflight_seen == ["codex"]


def test_explicit_auto_cli_overrides_seed_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit --cli auto wins over seed.cli when BERNSTEIN_ADAPTER is unset."""
    from bernstein.core.orchestration.preflight import resolve_effective_cli

    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    assert resolve_effective_cli("auto", "claude") == "auto"
    assert resolve_effective_cli(None, "claude") == "claude"


def test_per_role_cli_pin_fails_fast_before_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing role_model_policy.<role>.cli executable aborts before preview even if seed.cli exists."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    (tmp_path / "bernstein.yaml").write_text(
        'goal: "Ship"\ncli: codex\nrole_model_policy:\n  qa:\n    cli: gemini\n',
        encoding="utf-8",
    )
    _assert_aborts_before_preview(
        tmp_path,
        monkeypatch,
        which_fn=lambda name, path=None: "/usr/bin/codex" if name == "codex" else None,
        goal=None,
    )


def test_plan_step_cli_pin_fails_fast_before_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing per-step cli: executable in PLAN_FILE aborts before preview."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        'name: "Plan"\nstages:\n  - name: build\n    steps:\n      - title: Build widget\n        role: backend\n        cli: gemini\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    other = SimpleNamespace(name="codex", available_models=["gpt-5.4"], logged_in=True)
    _assert_aborts_before_preview(
        tmp_path,
        monkeypatch,
        discovery_agents=[other],
        which_fn=lambda name, path=None: "/usr/bin/codex" if name == "codex" else None,
        goal=None,
        plan_file=plan,
    )
