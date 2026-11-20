"""Exercise startup refusal and validation output without launching a worker."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_CLI = """
import sys
from unittest.mock import patch
from bernstein.cli.main import cli

failure = RuntimeError("fixture bootstrap failure [offline]: port already in use")
with (
    patch("bernstein.core.bootstrap.bootstrap_from_goal", side_effect=failure),
    patch("bernstein.core.bootstrap.bootstrap_from_seed", side_effect=failure),
):
    cli.main(args=sys.argv[1:])
"""


def _invoke(
    args: list[str], cwd: Path, env: dict[str, str], *, stderr_tty: bool, script: str = _CLI
) -> tuple[int, bytes, bytes]:
    command = [sys.executable, "-c", script, *args]
    if not stderr_tty:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, input=b"", timeout=20)
        return result.returncode, result.stdout, result.stderr

    import errno
    from concurrent.futures import ThreadPoolExecutor

    master, slave = os.openpty()
    with os.fdopen(master, "rb", buffering=0) as reader, ThreadPoolExecutor(max_workers=1) as pool:

        def read_terminal() -> bytes:
            output = bytearray()
            while True:
                try:
                    chunk = reader.read(8192)
                except OSError as exc:
                    if exc.errno == errno.EIO:  # POSIX terminal closed after its last write
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
            return bytes(output)

        with os.fdopen(slave, "wb", buffering=0) as terminal:
            output = pool.submit(read_terminal)
            result = subprocess.run(
                command, cwd=cwd, env=env, input=b"", stdout=subprocess.PIPE, stderr=terminal, timeout=20
            )
        return result.returncode, result.stdout, output.result()


@pytest.mark.parametrize(
    "stderr_tty",
    [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "posix", reason="requires a pseudo-terminal"))],
    ids=["pipe", "tty"],
)
@pytest.mark.parametrize(
    "scenario,options,expected",
    [
        (
            "default-branch",
            ["--json", "run", "--goal", "fixture", "--auto-approve"],
            ["Refusing to start:", "'main'", "git checkout -b <branch>", "BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH=1"],
        ),
        (
            "invalid",
            ["--json", "run", "--dry-run"],
            ["Error: Cannot parse seed file", "Reason:", "Fix: Check bernstein.yaml syntax"],
        ),
        ("invalid", ["run", "--quiet", "--plan-only"], ["Error: Cannot parse seed file", "Reason:", "Fix:"]),
        ("invalid", ["--quiet", "run", "--auto-approve"], ["Error: Cannot parse seed file", "Reason:", "Fix:"]),
        ("missing", ["--headless", "run", "--auto-approve"], ["Error: No goal or seed file found", "Reason:", "Fix:"]),
        ("missing", ["--json", "run", "--plan-only"], ["Error: No goal or seed file found", "Fix:"]),
        (
            "goal",
            ["--json", "run", "--goal", "fixture", "--auto-approve"],
            [
                "Error: Bootstrap failed",
                "Reason: fixture bootstrap failure [offline]",
                "Fix:",
                "Suggestion: Port already in use",
            ],
        ),
        (
            "seed",
            ["--json", "run", "--auto-approve"],
            [
                "Error: Bootstrap failed",
                "Reason: fixture bootstrap failure [offline]",
                "Fix:",
                "Suggestion: Port already in use",
            ],
        ),
    ],
)
def test_machine_startup_refusals_have_plain_remedies(
    tmp_path: Path, scenario: str, options: list[str], expected: list[str], stderr_tty: bool
) -> None:
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        TERM="xterm-256color",
        COLORTERM="truecolor",
    )
    env.pop("NO_COLOR", None)
    if scenario == "default-branch":
        env.pop("BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH", None)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=env, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "Fixture",
            ],
            cwd=tmp_path,
            env=env,
            check=True,
        )
    elif scenario == "invalid":
        (tmp_path / "bernstein.yaml").write_text("goal: fixture\nbudget: []\n")
    elif scenario == "seed":
        (tmp_path / "bernstein.yaml").write_text("goal: fixture\n")

    status, stdout, stderr = _invoke(options, tmp_path, env, stderr_tty=stderr_tty)

    assert status == 1, (stdout, stderr)
    assert stdout == b""
    assert b"\x1b" not in stderr
    for text in expected:
        assert text.encode() in stderr
    assert not (tmp_path / ".sdd" / "runtime" / "server.pid").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires a pseudo-terminal")
def test_regular_seed_error_retains_terminal_styles(tmp_path: Path) -> None:
    env = dict(os.environ, HOME=str(tmp_path), TERM="xterm-256color", COLORTERM="truecolor")
    env.pop("NO_COLOR", None)
    (tmp_path / "bernstein.yaml").write_text("goal: fixture\nbudget: []\n")
    status, _, stderr = _invoke(["run", "--plan-only"], tmp_path, env, stderr_tty=True)
    assert status == 1
    assert b"Cannot parse seed file" in stderr
    assert b"\x1b[" in stderr


# Keep the real goal/seed bootstrap, port refusal, and readiness-poll deadline.
# The process table, socket, child server, discovery and clock are local fixtures.
_BOOTSTRAP = """
import socket, subprocess, sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
from bernstein.cli.main import cli
from bernstein.core.orchestration import preflight
from bernstein.core.server import server_launch

scenario = sys.argv[1]
sock = MagicMock(spec=socket)
if scenario == "port":
    sock.socket.return_value.__enter__.return_value.bind.side_effect = OSError("fixture port is occupied")
real_run = subprocess.run
def local_run(args, *rest, **kwargs):
    if args[0] == "lsof":
        return subprocess.CompletedProcess(args, 0, "", "")
    return real_run(args, *rest, **kwargs)

agent = SimpleNamespace(name="fixture", available_models=[], logged_in=True)
discovery = SimpleNamespace(agents=[] if scenario == "no-cli" else [agent], warnings=[])
clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, float("inf")]), sleep=Mock())
with (
    patch.object(preflight, "socket", sock),
    patch("subprocess.run", side_effect=local_run),
    patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery),
    patch("bernstein.core.orchestration.bootstrap.supervised_server", return_value=12345) as launch,
    patch.object(server_launch, "time", clock),
    patch.object(server_launch.httpx, "get", side_effect=httpx.ConnectError("fixture server is unavailable")) as probe,
):
    try:
        cli.main(args=sys.argv[2:])
    finally:
        if scenario == "timeout":
            assert launch.call_count == 1
            probe.assert_called_once()
            assert probe.call_args.args[0].endswith("/health/ready")
            clock.sleep.assert_called_once()
        else:
            launch.assert_not_called()
"""


@pytest.mark.parametrize(
    "stderr_tty",
    [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "posix", reason="requires a pseudo-terminal"))],
    ids=["pipe", "tty"],
)
@pytest.mark.parametrize(
    "scenario,source,mode,expected",
    [
        (
            "timeout",
            "goal",
            ["--json"],
            ("did not respond within", "Server process may have crashed", ".sdd/runtime/server.log"),
        ),
        (
            "timeout",
            "seed",
            ["--json"],
            ("did not respond within", "Server process may have crashed", ".sdd/runtime/server.log"),
        ),
        ("timeout", "goal", [], ("did not respond within", ".sdd/runtime/server.log")),
        ("timeout", "goal", ["--headless"], ("did not respond within", ".sdd/runtime/server.log")),
        ("port", "seed", ["--quiet"], ("Port already in use", "bernstein stop", "--port 18618")),
        ("no-cli", "goal", ["--json"], ("No CLI agents found. Install at least one:", "claude", "codex")),
        ("no-adapter", "goal", ["--json"], ("no adapter configured", "BERNSTEIN_ADAPTER")),
    ],
)
def test_real_bootstrap_failure_preserves_only_the_plain_remedy(
    tmp_path: Path, scenario: str, source: str, mode: list[str], expected: tuple[str, ...], stderr_tty: bool
) -> None:
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }
    if os.name == "nt":
        env.update(SYSTEMROOT=os.environ["SYSTEMROOT"], USERPROFILE=str(tmp_path))
    args = [scenario, *mode, "run", "--auto-approve", "--port", "18617", "--model", "sonnet"]
    if scenario in {"timeout", "port"}:
        args.append("--idle")
    if source == "seed":
        (tmp_path / "bernstein.yaml").write_text("goal: fixture\n")
    else:
        args.extend(["--goal", "fixture"])
    if scenario == "no-adapter":
        (tmp_path / ".sdd").mkdir()

    status, stdout, stderr = _invoke(args, tmp_path, env, script=_BOOTSTRAP, stderr_tty=stderr_tty)

    assert status == 1, (stdout, stderr)
    for part in expected:
        assert part.encode() in stderr
    if mode:
        assert stdout == b""
        assert b"\x1b" not in stderr
        for narration in (
            b"Goal: fixture",
            b"Mock adapter ready",
            b"Created .sdd",
            b"Workspace ready",
            b"Found: Fixture",
        ):
            assert narration not in stderr
    else:
        assert b"Mock adapter ready" in stdout
        if stderr_tty:
            assert b"\x1b[" in stderr
