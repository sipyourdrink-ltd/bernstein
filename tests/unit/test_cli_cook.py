"""Tests for the `bernstein cook` CLI command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.main import cli


def _write_recipe(path: Path) -> None:
    path.write_text(
        "name: Test Recipe\n"
        "stages:\n"
        '  - name: "Sprint 1"\n'
        "    steps:\n"
        '      - title: "Implement endpoint"\n'
        "        role: backend\n"
        '  - name: "Sprint 2"\n'
        "    depends_on: [Sprint 1]\n"
        "    steps:\n"
        '      - title: "Add tests"\n'
        "        role: qa\n",
        encoding="utf-8",
    )


def test_cook_dry_run_shows_plan_and_cost(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe.yaml"
    _write_recipe(recipe)

    runner = CliRunner()
    result = runner.invoke(cli, ["cook", str(recipe), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "Estimated total" in result.output
    assert "Sprint 1" in result.output
    assert "Sprint 2" in result.output


def test_cook_execution_prints_live_progress(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe.yaml"
    _write_recipe(recipe)

    statuses = [
        {"total": 2, "open": 1, "claimed": 1, "done": 0, "failed": 0, "total_cost_usd": 0.50},
        {"total": 2, "open": 0, "claimed": 0, "done": 2, "failed": 0, "total_cost_usd": 1.20},
    ]
    health = [
        {"agent_count": 1},
        {"agent_count": 0},
    ]
    tasks = [
        [
            {"title": "Implement endpoint", "status": "claimed"},
            {"title": "Add tests", "status": "open"},
        ],
        [
            {"title": "Implement endpoint", "status": "done"},
            {"title": "Add tests", "status": "done"},
        ],
    ]
    state = {"idx": 0}

    def _fake_server_get(path: str):  # type: ignore[no-untyped-def]
        if path == "/status":
            idx = min(state["idx"], len(statuses) - 1)
            payload = statuses[idx]
            state["idx"] += 1
            return payload
        idx = min(max(state["idx"] - 1, 0), len(statuses) - 1)
        if path == "/health":
            return health[idx]
        if path == "/tasks":
            return tasks[idx]
        return {}

    runner = CliRunner()
    with (
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
        patch("bernstein.cli.run_confirm.server_get", side_effect=_fake_server_get),
        patch("bernstein.cli.run_confirm.time.sleep", return_value=None),
    ):
        result = runner.invoke(cli, ["cook", str(recipe), "--timeout", "5"])

    assert result.exit_code == 0, result.output
    assert "Sprint 0/2" in result.output
    assert "Sprint 2/2" in result.output
    assert "Recipe finished:" in result.output
