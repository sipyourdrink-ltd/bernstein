"""Tests for `bernstein ps` remote OpenClaw session visibility."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.status_cmd import ps_cmd
from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult


def _discovery_result() -> DiscoveryResult:
    return DiscoveryResult(
        agents=[
            AgentCapabilities(
                name="codex",
                binary="/usr/local/bin/codex",
                version="1.0.0",
                logged_in=True,
                login_method="ChatGPT",
                available_models=["gpt-5.4-mini", "gpt-5.4"],
                default_model="gpt-5.4",
                supports_headless=True,
                supports_sandbox=True,
                supports_mcp=True,
                max_context_tokens=200000,
                reasoning_strength="high",
                best_for=["code-review", "test-writing"],
                cost_tier="cheap",
            )
        ]
    )


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
    with patch("bernstein.cli.status_cmd.discover_agents_cached", return_value=_discovery_result()):
        result = runner.invoke(ps_cmd, ["--pid-dir", str(runtime_dir / "pids")], terminal_width=220)

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
    with patch("bernstein.cli.status_cmd.discover_agents_cached", return_value=_discovery_result()):
        result = runner.invoke(ps_cmd, ["--json-output", "--pid-dir", str(runtime_dir / "pids")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["runtime_backend"] == "openclaw"
    assert payload[0]["bridge_run_id"] == "run-999"
    assert payload[0]["skill_badges"] == ["reasoning:high", "mcp", "code-review", "test-writing"]
    assert "gpt-5.4-mini" in payload[0]["worker_badge"]
