"""End-to-end: ``bernstein task complete`` marks a task done on a live server.

Issue #3015. Boots a real uvicorn task server with bearer auth enabled, seeds
a task, then drives the ``bernstein task complete`` CLI in-process. The CLI is
given only what a spawned agent has - the server URL in the environment and the
session token in the persisted run-token file - and must resolve both itself.
No curl, no hand-built auth header, no JSON body.

Skipped on Windows for the same uvicorn/asyncio fixture reason as the sibling
manager-auth suite.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
import uvicorn
from click.testing import CliRunner
from fastapi import FastAPI

from bernstein.cli.commands.task_cmd import task_group
from bernstein.core.run_auth_token import persist_run_auth_token
from bernstein.core.server import create_app

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="uvicorn + asyncio + httpx fixtures fragile on Windows CI runners",
    ),
    pytest.mark.auth_enabled,
]

_BEARER_TOKEN = "regression-3015-bearer"


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _AuthServer:
    def __init__(self, app: FastAPI, port: int) -> None:
        self.app = app
        self.port = port
        self.endpoint = f"http://127.0.0.1:{port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self, *, timeout: float = 10.0) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name=f"taskserver-{self.port}")
        thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.02)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=2)
            raise RuntimeError(f"task server on port {self.port} did not start within {timeout}s")
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture
def auth_server(tmp_path: Path) -> Generator[_AuthServer, None, None]:
    jsonl_path = tmp_path / "server" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    app = create_app(jsonl_path=jsonl_path, auth_token=_BEARER_TOKEN)
    server = _AuthServer(app, _free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_cli_marks_task_complete_reading_token_itself(
    tmp_path: Path,
    auth_server: _AuthServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI completes a real task, reading the token from the run-token file."""
    # Seed a task on the live server (bearer supplied explicitly here - this is
    # the orchestrator's setup, not the agent path under test).
    create = httpx.post(
        f"{auth_server.endpoint}/tasks",
        json={"title": "demo", "role": "backend", "description": "issue-3015 e2e"},
        headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        timeout=5.0,
    )
    assert create.status_code in (200, 201), create.text
    task_id = create.json()["id"]

    # Agent-like environment: server URL in the env, token ONLY in the
    # persisted run-token file. BERNSTEIN_AUTH_TOKEN is deliberately unset so
    # the CLI must read the file itself.
    workdir = tmp_path / "agent-workspace"
    workdir.mkdir()
    persist_run_auth_token(workdir, _BEARER_TOKEN)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", auth_server.endpoint)
    monkeypatch.chdir(workdir)

    result = CliRunner().invoke(task_group, ["complete", task_id, "--summary", "Created hello.txt and committed"])
    assert result.exit_code == 0, result.output

    # The server now reports the task terminal (done).
    fetched = httpx.get(
        f"{auth_server.endpoint}/tasks/{task_id}",
        headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        timeout=5.0,
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "done", fetched.json()


def test_cli_rejects_when_token_missing(
    tmp_path: Path,
    auth_server: _AuthServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no token anywhere the live server rejects and the CLI exits non-zero."""
    create = httpx.post(
        f"{auth_server.endpoint}/tasks",
        json={"title": "demo2", "role": "backend", "description": "issue-3015 e2e auth"},
        headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        timeout=5.0,
    )
    assert create.status_code in (200, 201), create.text
    task_id = create.json()["id"]

    workdir = tmp_path / "no-token-workspace"
    workdir.mkdir()
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", auth_server.endpoint)
    monkeypatch.chdir(workdir)

    result = CliRunner().invoke(task_group, ["complete", task_id, "--summary", "should not land"])
    assert result.exit_code == 1
