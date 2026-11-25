"""Catalog-wide integrity checks across workspace and wheel-packaged templates."""

from __future__ import annotations

from pathlib import Path
import tomllib

from click.testing import CliRunner
import pytest

from bernstein.cli.main import cli
from bernstein.core.planning.scenario_library import load_scenario_library
from bernstein.core.teams.drift import compute_role_digests
from bernstein.core.workflows.workflow_spec import load_workflow_spec_from_text

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "pyproject.toml").is_file(),
    reason="template catalog checks only run in a source checkout",
)


def test_all_workspace_and_packaged_scenarios_parse_cleanly() -> None:
    ws_root = REPO_ROOT / ".bernstein" / "scenarios"
    pkg_root = REPO_ROOT / "templates" / "scenarios"

    ws_files = sorted(ws_root.rglob("*.yaml"))
    pkg_files = sorted(pkg_root.glob("*.yaml"))
    assert len(ws_files) >= 38
    assert len(pkg_files) >= 24

    ws_lib = load_scenario_library(ws_root)
    pkg_lib = load_scenario_library(pkg_root)
    assert len(ws_lib.scenarios) == len(ws_files)
    assert len(pkg_lib.scenarios) == len(pkg_files)

    for sc_id, scenario in {**ws_lib.scenarios, **pkg_lib.scenarios}.items():
        assert scenario.tasks, f"Scenario {sc_id} has no tasks"


def test_all_workspace_conditional_workflows_dry_run_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf_dir = REPO_ROOT / ".bernstein" / "workflows"
    wf_files = sorted(wf_dir.glob("*.yaml"))
    assert len(wf_files) >= 16

    monkeypatch.chdir(REPO_ROOT)
    runner = CliRunner(env={"COLUMNS": "200"})
    listed = runner.invoke(cli, ["workflow", "list"])
    assert listed.exit_code == 0, listed.output

    for wf_file in wf_files:
        res = runner.invoke(cli, ["workflow", "run", wf_file.stem, "--dry-run"])
        assert res.exit_code == 0, f"{wf_file.name} failed dry-run:\n{res.output}"


def test_all_packaged_workflow_specs_and_curated_teams_validate() -> None:
    tpl_wf_dir = REPO_ROOT / "templates" / "workflows"
    tpl_wf_files = sorted(tpl_wf_dir.glob("*.yaml"))
    assert len(tpl_wf_files) >= 15

    for wf_file in tpl_wf_files:
        spec = load_workflow_spec_from_text(wf_file.read_text(encoding="utf-8"))
        assert spec.name == wf_file.stem
        assert spec.nodes

    roles_dir = REPO_ROOT / "templates" / "roles"
    curated_teams = (
        "fullstack",
        "security-audit",
        "research-ops",
        "ai-governance",
        "platform-sre",
        "data-migration",
    )
    for team_name in curated_teams:
        manifest_path = REPO_ROOT / "templates" / "teams" / f"{team_name}.toml"
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        role_names = [entry["role"] for entry in data["roles"]]
        expected_digests = compute_role_digests(roles_dir, role_names)
        assert data["role_template_digests"] == expected_digests, f"Digest drift in {team_name}.toml"
