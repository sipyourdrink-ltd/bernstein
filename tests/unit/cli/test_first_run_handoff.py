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
    assert "git switch -c bernstein/first-run-2." in checkout_handoff(tmp_path).next_action
    _git(tmp_path, "switch", "-c", "work")
    assert checkout_handoff(tmp_path).next_action == ""
    _git(tmp_path, "switch", "--detach")
    assert "detached" in checkout_handoff(tmp_path).next_action.lower()
    assert "git switch -c bernstein/first-run-2." in checkout_handoff(tmp_path).next_action


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
    binary = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    binary.write_text("")
    binary.chmod(0o755)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "claude auth login" in worker_handoff("claude")
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


def test_version_output_does_not_establish_worker_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.first_run_readiness import worker_handoff

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    for authenticated in (False, True):
        status = '{"loggedIn":' + str(authenticated).lower() + "}"
        if os.name == "nt":
            script = f'@echo off\nif "%1"=="--version" (echo 2.0.0) else (echo {status})\n'
        else:
            script = f"""#!/bin/sh\ncase "$1" in --version) echo 2.0.0;; auth) echo '{status}';; esac\n"""
        fake.write_text(script)
        fake.chmod(0o755)
        action = worker_handoff("claude")
        if authenticated:
            assert action == ""
        else:
            assert "claude auth login" in action


def test_installation_readiness_requires_matching_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.first_run_readiness import operator_installation_checks

    monkeypatch.setenv("HOME", str(tmp_path))
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path), "OPENAI_API_KEY": "offline-fixture"}
    legacy = [
        {"name": "Python version", "ok": True},
        {"name": "Adapter: claude", "ok": True},
        {"name": "Auth: claude", "ok": True},
        {"name": "Adapter: codex", "ok": False},
        {"name": "Auth: codex", "ok": True},
        {"name": "Ready to run", "ok": True},
    ]
    rows = operator_installation_checks(legacy, env=env)
    assert not next(r for r in rows if r["name"] == "Ready to run")["ok"]
    assert legacy[-1]["ok"]


def _init_worker_project(tmp_path: Path, worker: str) -> None:
    _git(tmp_path, "init", "-b", "work")
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
    (tmp_path / "bernstein.yaml").write_text(
        f"goal: test\ncli: {worker}\nmodel: fixture\ninternal_llm_provider: none\n"
    )


def test_gemini_requires_its_own_setup_for_project_and_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.cli.first_run_readiness import operator_installation_checks, project_handoff

    _init_worker_project(tmp_path, "gemini")
    config = tmp_path / ".config" / "gemini"
    config.mkdir(parents=True)
    (config / "settings.json").write_text("{}")
    binary = tmp_path / ("gemini.cmd" if os.name == "nt" else "gemini")
    binary.write_text("")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path), "ANTHROPIC_API_KEY": "offline-fixture"}
    observed = [
        {"name": "Python version", "ok": True},
        {"name": "Adapter: gemini", "ok": True},
        {"name": "Auth: gemini", "ok": True, "detail": "config"},
        {"name": "Auth: claude", "ok": True},
        {"name": "Ready to run", "ok": True},
    ]
    for has_gemini_key in (False, True):
        if has_gemini_key:
            env["GEMINI_API_KEY"] = "offline-fixture"
        setup = project_handoff(tmp_path, worker_env=env)
        assert setup.worker == "gemini"
        assert (not setup.actions) == has_gemini_key
        if not has_gemini_key:
            assert any("Gemini sign-in" in step for step in setup.actions)
        checks = {row["name"]: row for row in operator_installation_checks(observed, env=env)}
        assert checks["Auth: gemini"]["ok"] == has_gemini_key
        assert checks["Ready to run"]["ok"] == has_gemini_key


def test_codex_custom_provider_without_first_party_login(tmp_path: Path) -> None:
    from bernstein.cli.first_run_readiness import operator_installation_checks, project_handoff

    _init_worker_project(tmp_path, "codex")
    binary = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    binary.write_text("@exit /b 1" if os.name == "nt" else "#!/bin/sh\nexit 1\n")
    binary.chmod(0o755)
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    observations = [
        {"name": "Python version", "ok": True},
        {"name": "Adapter: codex", "ok": True},
        {"name": "Auth: codex", "ok": True},
        {"name": "Ready to run", "ok": True},
    ]
    custom = (
        'model_provider = "fixture"\n[model_providers.fixture]\nname = "Fixture"\n'
        'base_url = "http://127.0.0.1:1/v1"\nwire_api = "responses"\n'
    )
    for settings, ready in [
        ('model = "fixture"\n', False),
        (custom, True),
        (custom + 'env_key = "FIXTURE_KEY"\n', False),
        (custom + "requires_openai_auth = true\n", False),
    ]:
        config.write_text(settings)
        assert (not project_handoff(tmp_path, worker_env=env).actions) == ready
        checks = {row["name"]: row for row in operator_installation_checks(observations, env=env)}
        assert checks["Auth: codex"]["ok"] == ready
        assert checks["Ready to run"]["ok"] == ready


def test_other_workers_use_their_declared_binary_and_access(tmp_path: Path) -> None:
    from bernstein.cli.first_run_readiness import worker_handoff

    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    assert "Install aider" in worker_handoff("ollama", env=env)
    binary = tmp_path / ("aider.cmd" if os.name == "nt" else "aider")
    binary.write_text("")
    binary.chmod(0o755)
    assert worker_handoff("ollama", env=env) == ""
    assert "Could not confirm" in worker_handoff("aider", env=env)
    assert worker_handoff("aider", env={**env, "OPENAI_API_KEY": "offline-fixture"}) == ""


def test_preflight_never_needs_to_probe_for_example_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.run_preflight import _abort_if_default_branch_merge_target

    monkeypatch.setattr("bernstein.core.git_ops.current_branch", lambda root: "main")
    monkeypatch.setattr("bernstein.core.git_ops.protected_default_branches", lambda root: {"main"})
    monkeypatch.delenv("BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH", raising=False)

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr("bernstein.core.git_ops.run_git", timed_out)
    with pytest.raises(SystemExit) as result:
        _abort_if_default_branch_merge_target(tmp_path)
    assert result.value.code == 1


@pytest.mark.parametrize("filename", ["bernstein.yml", ".bernstein/bernstein.yml", ".bernstein/bernstein.yaml"])
def test_init_and_setup_respect_seed_selection(tmp_path: Path, filename: str) -> None:
    from bernstein.cli.first_run_readiness import project_handoff
    from bernstein.cli.main import cli

    selected = tmp_path / filename
    selected.parent.mkdir(exist_ok=True)
    selected.write_text("goal: existing\ncli: codex\nmodel: preserved\ninternal_llm_provider: none\n")
    if filename.startswith(".bernstein"):
        (tmp_path / "bernstein.yaml").write_text("goal: fallback\ncli: gemini\n")
    result = CliRunner().invoke(cli, ["init", "--dir", str(tmp_path), "--remote"])
    assert result.exit_code == 0, result.output
    assert project_handoff(tmp_path, remote=True).worker == "codex"
    if filename == "bernstein.yml":
        assert not (tmp_path / "bernstein.yaml").exists()


def test_malformed_budget_still_emits_setup_advice(tmp_path: Path) -> None:
    from bernstein.cli.first_run_readiness import project_handoff

    (tmp_path / "bernstein.yaml").write_text("goal: test\nbudget: []\n")
    assert any("config validate" in step for step in project_handoff(tmp_path).actions)
