"""Tests for ``bernstein slo`` CLI command (ROAD-150)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.slo_cmd import _load_offline, _render_burndown, slo_cmd

# ---------------------------------------------------------------------------
# _load_offline
# ---------------------------------------------------------------------------


class TestLoadOffline:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        assert _load_offline(str(tmp_path)) is None

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        (d / "slos.json").write_text("{not valid json", encoding="utf-8")
        assert _load_offline(str(tmp_path)) is None

    def test_returns_burndown_dict(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        data = {
            "slos": [{"name": "task_success", "current": 0.95}],
            "error_budget": {
                "total_tasks": 100,
                "failed_tasks": 5,
                "budget_total": 10,
                "budget_remaining": 5,
                "burn_rate": 0.5,
                "is_depleted": False,
            },
        }
        (d / "slos.json").write_text(json.dumps(data), encoding="utf-8")
        result = _load_offline(str(tmp_path))
        assert result is not None
        assert "slo_target" in result
        assert "burn_rate" in result
        assert "budget_fraction" in result
        assert "status" in result
        assert result["total_tasks"] == 100
        assert result["failed_tasks"] == 5

    def test_status_green_when_healthy(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        data = {
            "slos": [],
            "error_budget": {
                "total_tasks": 100,
                "failed_tasks": 5,
                "budget_total": 10,
                "budget_remaining": 5,
                "burn_rate": 0.5,
                "is_depleted": False,
            },
        }
        (d / "slos.json").write_text(json.dumps(data), encoding="utf-8")
        result = _load_offline(str(tmp_path))
        assert result is not None
        assert result["status"] == "green"

    def test_status_red_when_depleted(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        data = {
            "slos": [],
            "error_budget": {
                "total_tasks": 10,
                "failed_tasks": 10,
                "budget_total": 1,
                "budget_remaining": 0,
                "burn_rate": 10.0,
                "is_depleted": True,
            },
        }
        (d / "slos.json").write_text(json.dumps(data), encoding="utf-8")
        result = _load_offline(str(tmp_path))
        assert result is not None
        assert result["status"] == "red"


# ---------------------------------------------------------------------------
# _render_burndown (smoke tests — no exceptions)
# ---------------------------------------------------------------------------


class TestRenderBurndown:
    def _burndown_data(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "slo_target": 0.9,
            "slo_current": 0.942,
            "burn_rate": 0.3,
            "burn_rate_per_day": 0.05,
            "budget_fraction": 0.72,
            "budget_consumed_pct": 28.0,
            "days_to_breach": 6.1,
            "breach_projection": "SLO will breach in 6.1 days at current rate",
            "status": "green",
            "total_tasks": 50,
            "failed_tasks": 3,
            "sparkline": [
                {"timestamp": 1.0, "burn_rate": 0.2, "budget_fraction": 0.8, "slo_current": 0.95},
                {"timestamp": 2.0, "burn_rate": 0.3, "budget_fraction": 0.75, "slo_current": 0.94},
            ],
        }
        base.update(overrides)
        return base

    def test_renders_without_exception(self) -> None:
        _render_burndown(self._burndown_data())

    def test_renders_red_status_without_exception(self) -> None:
        _render_burndown(
            self._burndown_data(
                status="red",
                burn_rate=5.0,
                budget_fraction=0.0,
                budget_consumed_pct=100.0,
                days_to_breach=None,
                breach_projection="Error budget exhausted — SLO breached now",
            )
        )

    def test_renders_compact_without_exception(self) -> None:
        _render_burndown(self._burndown_data(), compact=True)

    def test_renders_empty_sparkline(self) -> None:
        _render_burndown(self._burndown_data(sparkline=[]))


# ---------------------------------------------------------------------------
# slo_cmd CLI integration
# ---------------------------------------------------------------------------


class TestSloCmd:
    def _burndown_data(self) -> dict[str, object]:
        return {
            "slo_target": 0.9,
            "slo_current": 0.95,
            "burn_rate": 0.5,
            "burn_rate_per_day": 0.01,
            "budget_fraction": 0.8,
            "budget_consumed_pct": 20.0,
            "days_to_breach": None,
            "breach_projection": "On track — error budget not at risk",
            "status": "green",
            "total_tasks": 20,
            "failed_tasks": 1,
            "sparkline": [],
            "history_size": 0,
        }

    def test_no_data_exits_with_error(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        runner = CliRunner()
        # Patch server_get so it returns None (server unreachable), and no offline file exists.
        with patch("bernstein.cli.slo_cmd.server_get", return_value=None):
            result = runner.invoke(slo_cmd, ["--workdir", str(tmp_path)])
        assert result.exit_code != 0

    def test_json_output_from_offline_file(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        data = {
            "slos": [],
            "error_budget": {
                "total_tasks": 50,
                "failed_tasks": 2,
                "budget_total": 5,
                "budget_remaining": 3,
                "burn_rate": 0.4,
                "is_depleted": False,
            },
        }
        (d / "slos.json").write_text(json.dumps(data), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(slo_cmd, ["--workdir", str(tmp_path), "--json"])
        assert result.exit_code == 0
        # Find JSON block in output (may have leading warning lines).
        out = result.output
        json_start = out.find("{")
        assert json_start != -1, f"No JSON found in output: {out!r}"
        parsed = json.loads(out[json_start:])
        assert "slo_target" in parsed
        assert "burn_rate" in parsed

    def test_formatted_output_from_offline_file(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        data = {
            "slos": [],
            "error_budget": {
                "total_tasks": 50,
                "failed_tasks": 2,
                "budget_total": 5,
                "budget_remaining": 3,
                "burn_rate": 0.4,
                "is_depleted": False,
            },
        }
        (d / "slos.json").write_text(json.dumps(data), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(slo_cmd, ["--workdir", str(tmp_path)])
        assert result.exit_code == 0
        # Should show the dashboard (burn-down related content)
        assert "SLO" in result.output or "Burn" in result.output or "Error" in result.output

    def test_compact_flag_does_not_crash(self, tmp_path: Path) -> None:
        d = tmp_path / ".sdd" / "metrics"
        d.mkdir(parents=True)
        data = {
            "slos": [],
            "error_budget": {
                "total_tasks": 10,
                "failed_tasks": 1,
                "budget_total": 1,
                "budget_remaining": 0,
                "burn_rate": 1.0,
                "is_depleted": False,
            },
        }
        (d / "slos.json").write_text(json.dumps(data), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(slo_cmd, ["--workdir", str(tmp_path), "--compact"])
        assert result.exit_code == 0
