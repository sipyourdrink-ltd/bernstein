"""End-to-end CLI surface for registered recipes (#2546).

Exercises ``bernstein recipes register / show --registered / history
--verify / pause / fire / resume / rollback / plan / apply`` against a
throwaway working directory, proving the verbs are wired and behave per the
acceptance criteria at the command layer.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.recipes_cmd import recipes_group

_MANIFEST = """\
name: nightly-triage
description: "Nightly triage recipe."
version: "1.0.0"
nodes:
  - id: triage
    command: "echo triage"
schedules:
  - kind: cron
    recurrence: "0 9 * * *"
    timezone: America/New_York
    dst_policy: post_transition
"""


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    recipes_dir = tmp_path / ".bernstein" / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    (recipes_dir / "nightly-triage.yaml").write_text(_MANIFEST, encoding="utf-8")
    old = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


def _run(args: list[str]) -> object:
    return CliRunner().invoke(recipes_group, args)


def _write_schedule_trigger(workdir: Path) -> None:
    """Wire a trigger that consumes schedule fires.

    ``recipes fire`` reports a dispatch only when the task-graph dispatcher
    submits work, so a test that expects a dispatched fire has to give the
    pipeline something to match.
    """
    config_dir = workdir / ".sdd" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "triggers.yaml").write_text(
        "triggers:\n"
        "  - name: recipe-fire\n"
        "    source: schedule\n"
        "    enabled: true\n"
        "    task:\n"
        '      title: "Recipe fire"\n'
        "      role: backend\n",
        encoding="utf-8",
    )


class TestRegisterFlow:
    def test_register_then_show_registered(self, workdir: Path) -> None:
        result = _run(["register", "nightly-triage"])
        assert result.exit_code == 0, result.output
        assert "Registered" in result.output
        assert "recipe_hash:" in result.output

        shown = _run(["show", "nightly-triage", "--registered"])
        assert shown.exit_code == 0, shown.output
        assert "active" in shown.output

    def test_history_verify_passes(self, workdir: Path) -> None:
        assert _run(["register", "nightly-triage"]).exit_code == 0
        result = _run(["history", "nightly-triage", "--verify"])
        assert result.exit_code == 0, result.output
        assert "verified" in result.output

    def test_pause_blocks_fire_then_resume(self, workdir: Path) -> None:
        _write_schedule_trigger(workdir)
        assert _run(["register", "nightly-triage"]).exit_code == 0
        assert _run(["pause", "nightly-triage"]).exit_code == 0
        fired = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired.exit_code == 0
        assert "Not fired" in fired.output

        assert _run(["resume", "nightly-triage"]).exit_code == 0
        fired2 = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired2.exit_code == 0, fired2.output
        assert "projection_hash:" in fired2.output
        assert "submitted: 1" in fired2.output

    def test_fire_that_submits_nothing_exits_nonzero(self, workdir: Path) -> None:
        # No trigger consumes the fire, so nothing is submitted. The command
        # must report that instead of claiming a successful run.
        assert _run(["register", "nightly-triage"]).exit_code == 0
        fired = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired.exit_code == 2, fired.output
        assert "Not fired" in fired.output
        assert "projection_hash:" not in fired.output

    def test_fire_unregistered_exits_nonzero(self, workdir: Path) -> None:
        result = _run(["fire", "does-not-exist", "--at", "1"])
        assert result.exit_code == 1

    def test_plan_is_reproducible(self, workdir: Path) -> None:
        first = _run(["plan", "nightly-triage"])
        second = _run(["plan", "nightly-triage"])
        assert first.exit_code == 0, first.output
        assert "plan_hash:" in first.output
        # The plan_hash line is stable across runs against the same state.
        first_hash = _extract_plan_hash(first.output)
        second_hash = _extract_plan_hash(second.output)
        assert first_hash == second_hash

    def test_apply_registers_then_no_change(self, workdir: Path) -> None:
        plan = _run(["plan", "nightly-triage"])
        plan_hash = _extract_plan_hash(plan.output)
        applied = _run(["apply", "--plan", plan_hash, "nightly-triage"])
        assert applied.exit_code == 0, applied.output
        assert "Applied" in applied.output
        assert _run(["show", "nightly-triage", "--registered"]).exit_code == 0


def _extract_plan_hash(output: str) -> str:
    for line in output.splitlines():
        if "plan_hash:" in line:
            return line.split("plan_hash:", 1)[1].strip()
    raise AssertionError(f"no plan_hash in output: {output!r}")
