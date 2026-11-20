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


def _invoke(args: list[str], cwd: Path, env: dict[str, str], *, stderr_tty: bool) -> tuple[int, bytes, bytes]:
    command = [sys.executable, "-c", _CLI, *args]
    if not stderr_tty:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, input=b"", timeout=20)
        return result.returncode, result.stdout, result.stderr

    import select

    master, slave = os.openpty()
    with os.fdopen(master, "rb", buffering=0) as reader, os.fdopen(slave, "wb", buffering=0) as terminal:
        result = subprocess.run(
            command, cwd=cwd, env=env, input=b"", stdout=subprocess.PIPE, stderr=terminal, timeout=20
        )
        stderr = bytearray()
        while select.select([reader], [], [], 0)[0]:
            stderr.extend(reader.read(4096))
    return result.returncode, result.stdout, bytes(stderr)


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
