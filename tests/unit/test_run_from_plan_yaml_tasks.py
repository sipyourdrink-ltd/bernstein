"""``bernstein run --from-plan plan.yaml`` must run the plan, not re-plan its name.

The branch read only the plan's ``name`` and called ``bootstrap_from_goal``
without ``tasks=``, so every stage, step, model pin and ``depends_on`` was
dropped while the command printed "Loaded plan from" and exited 0.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from bernstein.cli import run_bootstrap
from tests.unit.test_issue_3255_plan_only_honors_plan_file import (
    _no_execution,
    _run_impl_environment,
    _run_impl_kwargs,
)


@pytest.fixture(autouse=True)
def _no_wait_for_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run is mocked, so there is no run to wait for."""
    monkeypatch.setattr(run_bootstrap, "_finalize_run_output", lambda **_: None)


PLAN = """\
name: "Checkout hardening"
cli: codex
budget: "$5"
stages:
  - name: build
    steps:
      - title: Add the limiter
        role: backend
        model: opus
  - name: verify
    depends_on: [build]
    steps:
      - title: Cover the limiter
        role: qa
"""


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, **overrides: object) -> Any:
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(text, encoding="utf-8")
    kwargs = _run_impl_kwargs(goal=None, from_plan=plan, **overrides)
    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _seed, _port):
        run_bootstrap._run_impl(**kwargs)  # type: ignore[arg-type]
    return bootstrap_goal


def test_from_plan_yaml_passes_the_plan_tasks_to_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_goal = _run(tmp_path, monkeypatch, PLAN)

    kwargs = bootstrap_goal.call_args.kwargs
    assert kwargs["goal"] == "Checkout hardening"
    tasks = kwargs["tasks"]
    assert [t.title for t in tasks] == ["Add the limiter", "Cover the limiter"]
    assert tasks[0].model == "opus"
    # The verify stage depends on the build stage, resolved to the task id.
    assert tasks[1].depends_on == [tasks[0].id]


def test_from_plan_yaml_applies_plan_cli_and_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_goal = _run(tmp_path, monkeypatch, PLAN)

    assert bootstrap_goal.call_args.kwargs["cli"] == "codex"


@pytest.mark.parametrize("source_option", ["--from-plan", "--"])
@pytest.mark.parametrize("override", [False, True])
def test_json_preview_rerun_replays_the_original_yaml_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_option: str, override: bool
) -> None:
    from bernstein.cli.main import cli

    monkeypatch.chdir(tmp_path)
    name = "-execution plan.yaml"
    Path(name).write_text(PLAN, encoding="utf-8")
    runner = CliRunner()
    options = (
        ["--cli", "gemini", "--model", "fixture model", "--budget", "1usd", "--hard-budget", "2usd"] if override else []
    )
    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _, _):
        preview = runner.invoke(cli, ["--json", "run", "--plan-only", "--auto-approve", *options, source_option, name])
        assert preview.exit_code == 0, preview.output
        document = json.loads(preview.stdout)
        assert document["plan"] == Path(document["saved_plan"]).read_text(encoding="utf-8")
        command = document["plan"].split("<!-- Re-run with `", 1)[1].split("` -->", 1)[0]
        args = shlex.split(command)
        assert args[:2] == ["bernstein", "run"]
        assert args[-2:] == [source_option, name]
        assert "--auto-approve" not in args
        bootstrap_goal.assert_not_called()

        flags = Mock()
        monkeypatch.setattr(run_bootstrap, "_propagate_env_flags", flags)
        monkeypatch.setattr(run_bootstrap, "_abort_if_default_branch_merge_target", lambda _: None)
        monkeypatch.setattr(run_bootstrap, "_emit_preflight_runtime_warnings", lambda **_: None)
        execution = runner.invoke(cli, args[1:])
        assert execution.exit_code == 0, execution.output
        kwargs = bootstrap_goal.call_args.kwargs
        assert [task.title for task in kwargs["tasks"]] == ["Add the limiter", "Cover the limiter"]
        if override:
            assert kwargs["cli"] == "gemini"
            assert kwargs["model"] == "fixture model"
            assert flags.call_args.kwargs["max_cost_usd"] == 1.0
            assert flags.call_args.kwargs["hard_budget_usd"] == 2.0
        elif source_option == "--from-plan":
            assert kwargs["cli"] == "codex"
            assert flags.call_args.kwargs["max_cost_usd"] == 5.0


def test_yaml_preview_with_other_options_keeps_the_original_command_as_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.cli.main import cli

    monkeypatch.chdir(tmp_path)
    Path("plan.yaml").write_text(PLAN, encoding="utf-8")
    with _run_impl_environment(), _no_execution():
        result = CliRunner().invoke(cli, ["--json", "run", "--plan-only", "--activity-log", "events.json", "plan.yaml"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert "repeat your original command" in document["plan"].lower()
    assert "Re-run with `bernstein run plan.yaml`" not in document["plan"]


def test_yaml_preview_preserves_root_option_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.main import cli

    monkeypatch.chdir(tmp_path)
    Path("plan.yaml").write_text(PLAN, encoding="utf-8")
    runner = CliRunner()
    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _, _):
        preview = runner.invoke(cli, ["--json", "--plan-only", "--model", "fixture model", "--from-plan", "plan.yaml"])
        assert preview.exit_code == 0, preview.output
        command = json.loads(preview.stdout)["plan"].split("<!-- Re-run with `", 1)[1].split("` -->", 1)[0]
        args = shlex.split(command)
        assert args == ["bernstein", "--model", "fixture model", "--from-plan", "plan.yaml"]
        monkeypatch.setattr(run_bootstrap, "_abort_if_default_branch_merge_target", lambda _: None)
        monkeypatch.setattr(run_bootstrap, "_emit_preflight_runtime_warnings", lambda **_: None)
        execution = runner.invoke(cli, args[1:])
        assert execution.exit_code == 0, execution.output
        assert bootstrap_goal.call_args.kwargs["model"] == "fixture model"


def test_explicit_cli_flag_beats_plan_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_goal = _run(tmp_path, monkeypatch, PLAN, cli="claude")

    assert bootstrap_goal.call_args.kwargs["cli"] == "claude"


def test_from_plan_yaml_budget_reaches_the_run_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(PLAN, encoding="utf-8")
    seen: dict[str, Any] = {}

    def capture(**kwargs: Any) -> None:
        seen.update(kwargs)

    with _run_impl_environment(), _no_execution():
        monkeypatch.setattr(run_bootstrap, "_propagate_env_flags", capture)
        run_bootstrap._run_impl(**_run_impl_kwargs(goal=None, from_plan=plan))  # type: ignore[arg-type]

    assert seen["max_cost_usd"] == 5.0


@pytest.mark.parametrize("setting", ["max_agents: 4", "constraints: [no network]", "repos:\n  - path: ../other"])
def test_from_plan_refuses_plan_settings_the_run_cannot_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, capsys: pytest.CaptureFixture[str]
) -> None:
    text = PLAN + setting + "\n"
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(text, encoding="utf-8")

    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _seed, _port):
        with pytest.raises(SystemExit) as exc:
            run_bootstrap._run_impl(**_run_impl_kwargs(goal=None, from_plan=plan))  # type: ignore[arg-type]

    assert exc.value.code == 1
    bootstrap_goal.assert_not_called()
    output = capsys.readouterr()
    assert setting.split(":")[0] in output.err
    assert output.out == ""


def test_from_plan_refuses_a_seed_shaped_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    seed = tmp_path / "seed.yaml"
    seed.write_text("goal: Build a thing\n", encoding="utf-8")

    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _seed, _port):
        with pytest.raises(SystemExit) as exc:
            run_bootstrap._run_impl(**_run_impl_kwargs(goal=None, from_plan=seed))  # type: ignore[arg-type]

    assert exc.value.code == 1
    bootstrap_goal.assert_not_called()
    output = capsys.readouterr()
    assert "--seed" in output.err
    assert output.out == ""


def test_from_plan_markdown_still_runs_from_the_goal_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    saved = tmp_path / "plan.md"
    saved.write_text("# Plan\n\n**Goal:** Rotate the signing keys\n", encoding="utf-8")

    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _seed, _port):
        run_bootstrap._run_impl(**_run_impl_kwargs(goal=None, from_plan=saved))  # type: ignore[arg-type]

    assert bootstrap_goal.call_args.kwargs["goal"] == "Rotate the signing keys"
    assert "tasks" not in bootstrap_goal.call_args.kwargs
