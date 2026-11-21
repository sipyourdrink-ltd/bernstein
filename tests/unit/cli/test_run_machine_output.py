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
if outcome in {"recovered", "success"}:
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


@pytest.mark.skipif(os.name != "posix", reason="requires a pseudo-terminal")
@pytest.mark.parametrize("key", [b"y", b"n", None], ids=["approve", "decline", "ci"])
def test_json_confirmation_keeps_stdout_clean_on_a_terminal(tmp_path: Path, key: bytes | None) -> None:
    import select

    args = ["--json", "run", "--idle", "--model", "fixture", "--goal", "fixture"]
    env = _env(tmp_path)
    env.pop("CI", None)
    env.pop("GITHUB_ACTIONS", None)
    if key is None:
        env["CI"] = "1"

    master, slave = os.openpty()
    with os.fdopen(master, "r+b", buffering=0) as terminal, os.fdopen(slave, "r+b", buffering=0) as child_terminal:
        with subprocess.Popen(
            [sys.executable, "-c", _RUN, "success", *args],
            cwd=tmp_path,
            env=env,
            stdin=child_terminal,
            stdout=child_terminal,
            stderr=subprocess.PIPE,
        ) as process:
            assert process.stderr is not None
            prompt = b""
            stdout = b""
            try:
                if key is not None:
                    while b"to cancel." not in prompt:
                        assert select.select([process.stderr], [], [], 10)[0], prompt
                        chunk = os.read(process.stderr.fileno(), 4096)
                        assert chunk, prompt
                        prompt += chunk
                    terminal.write(key)
                _, stderr = process.communicate(timeout=15)
                while select.select([terminal], [], [], 0)[0]:
                    stdout += terminal.read(4096)
            finally:
                process.kill()
    assert process.returncode == 0, (stdout, prompt, stderr)
    assert b"\x1b" not in stdout + prompt + stderr
    if key is None:
        assert b"to approve" not in stderr
    else:
        assert b"Goal: fixture" in prompt
        assert b"Estimated cost:" in prompt
    if key == b"n":
        assert stdout == b""
        assert b"Cancelled." in stderr
    else:
        assert json.loads(stdout)["summary"]["done"] == 1
        if key is not None:
            assert b"Plan approved." in stderr


@pytest.mark.skipif(os.name != "posix", reason="requires a pseudo-terminal")
def test_json_redirected_stdout_keeps_noninteractive_approval(tmp_path: Path) -> None:
    args = ["--json", "run", "--idle", "--model", "fixture", "--goal", "fixture"]
    env = _env(tmp_path)
    env.pop("CI", None)
    env.pop("GITHUB_ACTIONS", None)
    master, slave = os.openpty()
    with os.fdopen(master, "rb"), os.fdopen(slave, "r+b", buffering=0) as terminal:
        result = subprocess.run(
            [sys.executable, "-c", _RUN, "success", *args],
            cwd=tmp_path,
            env=env,
            stdin=terminal,
            stderr=terminal,
            stdout=subprocess.PIPE,
            timeout=15,
            check=True,
        )
    assert json.loads(result.stdout)["summary"]["done"] == 1


def test_json_auto_approval_under_pipes_only_emits_summary(tmp_path: Path) -> None:
    args = ["--json", "run", "--idle", "--model", "fixture", "--goal", "fixture"]
    result = subprocess.run(
        [sys.executable, "-c", _RUN, "success", *args],
        cwd=tmp_path,
        env=_env(tmp_path),
        input=b"",
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout)["summary"]["done"] == 1
    assert b"to approve" not in result.stderr
    assert b"\x1b" not in result.stdout + result.stderr


def test_json_declined_without_goal_leaves_stdout_empty(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bernstein", "--json", "run", "--idle"],
        cwd=tmp_path,
        env=_env(tmp_path),
        input=b"n\n",
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert result.stdout == b""
    assert b"Proceed with execution?" in result.stderr
    assert b"Cancelled." in result.stderr


def test_json_budget_refusal_is_plain_stderr(tmp_path: Path) -> None:
    args = ["--json", "run", "--idle", "--goal", "fixture", "--budget-cap", "0.01", "--model", "sonnet"]
    result = subprocess.run(
        [sys.executable, "-m", "bernstein", *args],
        cwd=tmp_path,
        env=_env(tmp_path),
        input=b"",
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == b""
    assert b"Budget cap exceeded: p90 $" in result.stderr
    assert b"> cap $0.01. Aborting spawn." in result.stderr
    assert b"\x1b" not in result.stderr


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
