"""A directory whose name ends in ``.yaml`` is not a plan (#7943).

``list_plans`` globbed ``*.yaml`` and ``find`` checked ``exists()``, so ``plans/active/notes.yaml/``
was listed as a plan and ``plan show notes`` raised ``IsADirectoryError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.plan_archive_cmd import plan_ls, plan_show
from bernstein.core.planning.lifecycle import PlanLifecycle, PlanState

PLAN = "name: garden\nstages: []\n"


@pytest.fixture
def plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    active = tmp_path / "plans" / "active"
    active.mkdir(parents=True)
    (active / "notes.yaml").mkdir()
    (active / "garden.yaml").write_text(PLAN)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "plans"


def test_list_skips_a_yaml_named_directory(plans: Path) -> None:
    ids = [p.plan_id for p in PlanLifecycle(plans).list_plans(PlanState.ACTIVE)]
    assert ids == ["garden"]


def test_find_skips_a_yaml_named_directory(plans: Path) -> None:
    lifecycle = PlanLifecycle(plans)
    assert lifecycle.find("notes") is None
    found = lifecycle.find("garden")
    assert found is not None
    assert found.path.read_text() == PLAN


def test_plan_ls_lists_only_the_plan_file(plans: Path) -> None:
    result = CliRunner().invoke(plan_ls, ["--state", "active"], terminal_width=200)
    assert result.exit_code == 0
    assert "garden" in result.output
    assert "notes" not in result.output


def test_plan_show_reports_no_plan_for_the_directory(plans: Path) -> None:
    result = CliRunner().invoke(plan_show, ["notes"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, IsADirectoryError)
    assert "No plan" in result.output


def test_plan_show_still_reads_the_plan_file(plans: Path) -> None:
    result = CliRunner().invoke(plan_show, ["garden"])
    assert result.exit_code == 0
    assert "name: garden" in result.output
