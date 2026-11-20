"""Machine output from the real CLI when startup or the run ends unsuccessfully."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_RUN = """
import sys, time
from types import SimpleNamespace
from unittest.mock import patch

from bernstein.cli.main import cli
from bernstein.cli import run_bootstrap as rb
from bernstein.core.config.seed import SeedConfig

outcome = sys.argv[1]
agent_count = int(outcome not in {"refused", "recovered", "in_progress"})
status = {"total": 1, "failed": 1, "done": 0}
if outcome == "recovered":
    status = {"total": 1, "failed": 0, "done": 1}
elif outcome == "in_progress":
    status = {"total": 2, "failed": 0, "done": 0, "in_progress": 2}
summary = {"summary": status, "agents": []}
reason = "Spawn failed: local adapter could not start"
if outcome in {"recovered", "in_progress"}:
    reason += " (transient: retry pending)"

def server_get(path):
    if path == "/health":
        return {"agent_count": agent_count}
    if path == "/tasks?status=failed&limit=50":
        return {"tasks": [{"result_summary": reason, "completed_at": time.time()}]}
    if path == "/status":
        return summary
    return {}

def actual_outcome(**kwargs):
    assert not kwargs.get("narrate_wait"), "machine mode requested a Rich wait indicator"
    return original(timeout_s=0, **kwargs)

original = rb._await_first_spawn_outcome
seed = SeedConfig(goal="fixture", cli="mock", model="fixture")
with (
    patch("bernstein.core.bootstrap.bootstrap_from_goal", return_value=SimpleNamespace(seed=seed)),
    patch("bernstein.cli.run_bootstrap._await_first_spawn_outcome", side_effect=actual_outcome),
    patch("bernstein.cli.run_bootstrap.server_get", side_effect=server_get),
    patch("bernstein.cli.helpers.server_get", side_effect=server_get),
    patch("bernstein.cli.run_bootstrap._wait_for_run_completion", return_value=None if outcome == "in_progress" else summary),
    patch("bernstein.cli.run_bootstrap._poll_quiescent_status", return_value=summary),
    patch("bernstein.cli.run_bootstrap._poll_no_plan_after_spawn", return_value="no plan" if outcome == "no_plan" else None),
    patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=SimpleNamespace(is_tty=True, supports_textual=True)),
):
    cli.main(args=sys.argv[2:])
"""


def _env(workdir: Path) -> dict[str, str]:
    return dict(os.environ, HOME=str(workdir), XDG_CONFIG_HOME=str(workdir / "config"), TERM="xterm-256color")


@pytest.mark.parametrize("flags", [[], ["--wait", "1"], ["--quiet"]])
@pytest.mark.parametrize("outcome", ["refused", "unhealthy", "no_plan"])
def test_failed_run_keeps_one_json_document(tmp_path: Path, flags: list[str], outcome: str) -> None:
    args = ["--json", "run", "--idle", "--auto-approve", "--model", "fixture", "--goal", "fixture", *flags]
    result = subprocess.run(
        [sys.executable, "-c", _RUN, outcome, *args], cwd=tmp_path, env=_env(tmp_path), capture_output=True, timeout=20
    )
    expected_exit = {"refused": 3 if flags else 1, "unhealthy": 3, "no_plan": 70}[outcome]
    assert result.returncode == expected_exit, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["summary"]["failed"] == 1
    expected = {
        "refused": b"local adapter could not start",
        "unhealthy": b"Run did not meet its goal",
        "no_plan": b"no work plan was produced",
    }[outcome]
    assert expected in b" ".join(result.stderr.split())
    assert "╭".encode() not in result.stderr
    assert b"retrospective.md" in result.stderr
    assert b"\x1b" not in result.stdout + result.stderr


def test_bare_json_recovers_orphan_without_mixing_output(tmp_path: Path) -> None:
    claimed = tmp_path / ".sdd" / "backlog" / "claimed"
    claimed.mkdir(parents=True)
    (claimed / "400-orphan.md").write_text("# Orphan")
    result = subprocess.run(
        [sys.executable, "-m", "bernstein", "--json", "--plan-only", "--goal", "fixture"],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert "plan" in json.loads(result.stdout)
    assert b"Recovered 1 orphaned ticket" in result.stderr
    assert b"\x1b" not in result.stdout + result.stderr
    assert (claimed.parent / "open" / "400-orphan.md").is_file()


def test_interrupt_inside_run_uses_click_cancellation(tmp_path: Path) -> None:
    script = """
from unittest.mock import patch
from bernstein.cli.main import cli
with patch("bernstein.cli.run_bootstrap._run_impl", side_effect=KeyboardInterrupt):
    cli.main(args=["--json", "run", "--goal", "fixture"])
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=_env(tmp_path), capture_output=True, timeout=20
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Aborted" in result.stderr
    assert b"Unhandled error" not in result.stderr


@pytest.mark.parametrize("mode", ["--headless", "--quiet"])
def test_headless_and_quiet_keep_refusal_in_plain_stderr(tmp_path: Path, mode: str) -> None:
    args = [mode, "run", "--idle", "--auto-approve", "--model", "fixture", "--goal", "fixture"]
    result = subprocess.run(
        [sys.executable, "-c", _RUN, "refused", *args],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert b"local adapter could not start" in result.stderr
    assert b"Total tasks" in result.stdout
    assert b"\x1b" not in result.stdout + result.stderr
    assert b"Bernstein " not in result.stdout


@pytest.mark.parametrize("flags", [["--wait", "1"], ["--quiet"]])
@pytest.mark.parametrize("outcome", ["recovered", "in_progress"])
def test_wait_keeps_observing_after_retryable_spawn_failure(tmp_path: Path, flags: list[str], outcome: str) -> None:
    args = ["--json", "run", "--idle", "--auto-approve", "--model", "fixture", "--goal", "fixture", *flags]
    result = subprocess.run(
        [sys.executable, "-c", _RUN, outcome, *args], cwd=tmp_path, env=_env(tmp_path), capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)["summary"]
    assert summary["done"] == 1 if outcome == "recovered" else summary["in_progress"] == 2
    assert b"retry pending" in result.stderr
    assert b"Run failed before any work started" not in result.stderr
