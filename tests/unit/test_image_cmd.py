"""The published image CMD must target a real, foreground-capable command.

Regression guard for #2803. The GHCR image shipped ``CMD ["conduct"]`` even
though no ``conduct`` command is registered on the top-level CLI group, so a
bare ``docker run <image>`` failed with a click "No such command 'conduct'"
error. The image also had no foreground server mode: ``run`` / ``start`` detach
the task server as a background process and return, so as PID 1 in a container
the CLI exits immediately and the container dies before ``/health`` is ever
reachable.

These tests pin two invariants:

1. The Dockerfile ``CMD`` names a command that is actually registered on the
   ``bernstein`` CLI group.
2. A foreground task-server command exists and launches the real task-server
   ASGI app in-process (blocking) rather than detaching.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.main import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _dockerfile_cmd_tokens() -> list[str]:
    """Return the exec-form (JSON array) tokens of the Dockerfile ``CMD``.

    Only the top-level ``CMD`` instruction (column 0) is considered; the
    indented ``CMD`` that belongs to the ``HEALTHCHECK`` directive is skipped.
    """
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("CMD "):
            tokens = json.loads(raw[len("CMD ") :].strip())
            assert isinstance(tokens, list), "Dockerfile CMD must use exec (JSON array) form"
            return [str(token) for token in tokens]
    raise AssertionError("Dockerfile has no top-level CMD directive")


def test_dockerfile_cmd_targets_a_registered_cli_command() -> None:
    """`docker run <image>` (no args) must resolve to a real subcommand."""
    tokens = _dockerfile_cmd_tokens()
    assert tokens, "Dockerfile CMD must not be empty"
    target = tokens[0]
    registered = set(cli.commands)
    assert target in registered, (
        f"Dockerfile CMD invokes 'bernstein {target}', but no such command is "
        f"registered on the CLI group (available: {sorted(registered)})"
    )


def test_foreground_serve_command_is_registered() -> None:
    """A foreground task-server command must exist so the image can host a node."""
    assert "serve" in cli.commands, "a foreground task-server command 'serve' must be registered"


def test_serve_launches_task_server_in_the_foreground() -> None:
    """`serve` runs the real task-server app via in-process uvicorn (no detach)."""
    serve = cli.commands["serve"]
    with patch("uvicorn.run") as run_mock:
        result = CliRunner().invoke(serve, ["--port", "9099"])

    assert result.exit_code == 0, result.output
    assert run_mock.call_count == 1, "serve must launch exactly one foreground server"
    args, kwargs = run_mock.call_args
    app_ref = args[0] if args else kwargs.get("app")
    # Foreground == in-process uvicorn against the task-server ASGI app; the
    # detached path (Popen(start_new_session=True)) never calls uvicorn.run.
    assert app_ref == "bernstein.core.server:app"
    assert kwargs.get("port") == 9099
