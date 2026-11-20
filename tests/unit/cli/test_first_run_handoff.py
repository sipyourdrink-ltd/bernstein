"""Setup advice for a specific project, independent of the operator machine."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.init_wizard_cmd import init_wizard_cmd


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _clean_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_wizard_rerun_preserves_operator_configuration(tmp_path: Path) -> None:
    config = tmp_path / "bernstein.yaml"
    original = "goal: Keep my settings\ncli: codex\nbudget: 3\n"
    config.write_text(original)
    result = CliRunner().invoke(init_wizard_cmd, ["--non-interactive", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert config.read_text() == original


@pytest.mark.parametrize("branch", ["main", "master", "release"])
def test_checkout_advice_tracks_the_actual_default(tmp_path: Path, branch: str) -> None:
    from bernstein.cli.first_run_readiness import checkout_handoff

    _git(tmp_path, "init", "-b", branch)
    _git(
        tmp_path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    _git(tmp_path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    _git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    result = checkout_handoff(tmp_path)
    assert result.branch == branch
    assert result.default_target
    assert "git switch -c" in result.next_action
    _git(tmp_path, "branch", "bernstein/first-run")
    assert "git switch bernstein/first-run" in checkout_handoff(tmp_path).next_action
    _git(tmp_path, "switch", "-c", "work")
    assert checkout_handoff(tmp_path).next_action == ""
    _git(tmp_path, "switch", "--detach")
    assert "detached" in checkout_handoff(tmp_path).next_action.lower()


def test_unborn_and_non_git_have_different_repairs(tmp_path: Path) -> None:
    from bernstein.cli.first_run_readiness import checkout_handoff

    assert "git init" in checkout_handoff(tmp_path).next_action
    _git(tmp_path, "init", "-b", "main")
    assert "commit" in checkout_handoff(tmp_path).next_action


def test_setup_declares_missing_worker_before_its_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.first_run_readiness import worker_handoff

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-only")
    assert "install" in worker_handoff("claude").lower()
    binary = tmp_path / "claude"
    binary.write_text("")
    binary.chmod(0o755)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("bernstein.core.orchestration.preflight._claude_has_oauth_session", lambda **kw: False)
    assert "claude login" in worker_handoff("claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-only")
    assert worker_handoff("claude") == ""


def test_planner_is_checked_separately_from_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.first_run_readiness import planner_handoff

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-only")
    monkeypatch.delenv("OPENROUTER_API_KEY_FREE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_PAID", raising=False)
    assert "openrouter_free" in planner_handoff("openrouter_free")
    assert planner_handoff("none") == ""


def test_invalid_config_supplies_a_repair_without_echoing_values(tmp_path: Path) -> None:
    from bernstein.cli.first_run_readiness import project_handoff

    (tmp_path / "bernstein.yaml").write_text("goal: [not, a, string]\n")
    handoff = project_handoff(tmp_path)
    assert handoff.worker is None
    assert any("config validate" in step for step in handoff.actions)
    assert "not, a, string" not in str(handoff)


def test_repeat_init_preserves_selected_branch_budget_and_local_settings(tmp_path: Path) -> None:
    from bernstein.cli.main import cli

    _git(tmp_path, "init", "-b", "dev")
    original = "goal: A specific goal\ncli: codex\nmodel: gpt-5.4\ninternal_llm_provider: none\nbudget: 3.17\n"
    (tmp_path / "bernstein.yaml").write_text(original)
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    state = runtime / "workspace_trust.json"
    state.write_bytes(b"unchanged fixture")
    for _ in range(2):
        result = CliRunner().invoke(cli, ["init", "--dir", str(tmp_path), "--remote"])
        assert result.exit_code == 0, result.output
    assert (tmp_path / "bernstein.yaml").read_text() == original
    assert state.read_bytes() == b"unchanged fixture"
    branch = subprocess.check_output(["git", "symbolic-ref", "--short", "HEAD"], cwd=tmp_path, text=True)
    assert branch.strip() == "dev"
