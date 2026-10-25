"""``bernstein run --from-plan plan.yaml`` must run the plan, not re-plan its name.

The branch read only the plan's ``name`` and called ``bootstrap_from_goal``
without ``tasks=``, so every stage, step, model pin and ``depends_on`` was
dropped while the command printed "Loaded plan from" and exited 0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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
    assert setting.split(":")[0] in capsys.readouterr().out


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
    assert "--seed" in capsys.readouterr().out.replace("\n", " ")


def test_from_plan_markdown_still_runs_from_the_goal_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    saved = tmp_path / "plan.md"
    saved.write_text("# Plan\n\n**Goal:** Rotate the signing keys\n", encoding="utf-8")

    with _run_impl_environment(), _no_execution() as (bootstrap_goal, _seed, _port):
        run_bootstrap._run_impl(**_run_impl_kwargs(goal=None, from_plan=saved))  # type: ignore[arg-type]

    assert bootstrap_goal.call_args.kwargs["goal"] == "Rotate the signing keys"
    assert "tasks" not in bootstrap_goal.call_args.kwargs
