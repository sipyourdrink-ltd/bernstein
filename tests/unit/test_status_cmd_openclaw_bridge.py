"""Tests for `bernstein ps` remote OpenClaw session visibility."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.status_cmd import ps_cmd


def test_ps_shows_remote_sessions_from_agents_snapshot(tmp_path: Path) -> None:
    """Remote bridge sessions should appear even without PID files."""
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "agents.json").write_text(
        json.dumps(
            {
                "ts": 1,
                "agents": [
                    {
                        "id": "backend-1234",
                        "role": "backend",
                        "model": "gpt-5.4-mini",
                        "spawn_ts": 1,
                        "runtime_backend": "openclaw",
                        "bridge_session_key": "agent:ops:bernstein-backend-1234",
                        "bridge_run_id": "run-123",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(ps_cmd, ["--pid-dir", str(runtime_dir / "pids")])

    assert result.exit_code == 0
    assert "backend-1234" in result.output
    assert "1 agent(s) running" in result.output


def test_ps_json_includes_remote_snapshot_entries(tmp_path: Path) -> None:
    """JSON output should include remote backend metadata from agents.json."""
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "agents.json").write_text(
        json.dumps(
            {
                "ts": 1,
                "agents": [
                    {
                        "id": "qa-9999",
                        "role": "qa",
                        "model": "gpt-5.4-mini",
                        "spawn_ts": 1,
                        "runtime_backend": "openclaw",
                        "bridge_session_key": "agent:ops:bernstein-qa-9999",
                        "bridge_run_id": "run-999",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(ps_cmd, ["--json-output", "--pid-dir", str(runtime_dir / "pids")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["runtime_backend"] == "openclaw"
    assert payload[0]["bridge_run_id"] == "run-999"
