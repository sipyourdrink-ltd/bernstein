"""Tests for `bernstein doctor --failover-drill` (issue #2355).

The drill exercises every declared per-role fallback path with the live
probe set and exits non-zero when any declared chain is broken, so operators
find dead chains before an outage does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from bernstein.cli.main import cli

_HEALTHY_CONFIG = {
    "goal": "test goal",
    "provider_availability": {
        "roles": {
            "developer": {
                "conformance_floor": "basic",
                "chain": [
                    # git is a hard prerequisite of the project: always on PATH.
                    {"adapter": "git", "model": "n/a", "conformance": "basic"},
                ],
            },
        },
    },
}

_BROKEN_CONFIG = {
    "goal": "test goal",
    "provider_availability": {
        "roles": {
            "developer": {
                "conformance_floor": "basic",
                "chain": [
                    {"adapter": "git", "model": "n/a", "conformance": "basic"},
                    {"adapter": "definitely-not-a-real-binary-2355", "model": "x", "conformance": "basic"},
                ],
            },
        },
    },
}


def _write_config(path: Path, config: dict) -> None:
    (path / "bernstein.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_failover_drill_flag_is_documented() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--failover-drill" in result.output


def test_failover_drill_passes_when_all_chains_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, _HEALTHY_CONFIG)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--failover-drill"])
    assert result.exit_code == 0, result.output
    assert "developer" in result.output


def test_failover_drill_exits_nonzero_on_broken_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, _BROKEN_CONFIG)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--failover-drill"])
    assert result.exit_code == 1, result.output
    assert "BROKEN" in result.output
    assert "Broken chains for roles: developer" in result.output


def test_failover_drill_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _write_config(tmp_path, _BROKEN_CONFIG)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--failover-drill", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["broken_roles"] == ["developer"]
    rows = payload["elements"]
    assert len(rows) == 2
    assert rows[0]["healthy"] is True
    assert rows[1]["healthy"] is False
    assert rows[0]["decision_hash"].startswith("sha256:")


def test_failover_drill_without_declared_chains_reports_nothing_to_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, {"goal": "test goal"})
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--failover-drill"])
    assert result.exit_code == 0, result.output
    assert "no fallback chains" in result.output.lower()


def test_failover_drill_rejects_below_floor_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "goal": "test goal",
        "provider_availability": {
            "roles": {
                "developer": {
                    "conformance_floor": "expert",
                    "chain": [{"adapter": "git", "model": "n/a", "conformance": "basic"}],
                },
            },
        },
    }
    _write_config(tmp_path, bad)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--failover-drill"])
    assert result.exit_code == 1, result.output
    assert "conformance" in result.output.lower()
