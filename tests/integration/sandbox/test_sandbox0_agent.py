"""Opt-in real CLI/LLM smoke test, including an authenticated remote callback.

Run on an isolated host reachable from the sandbox. Requires a template with
Python, Git, and the CLI used by the test's adapter, plus
OPENAI_API_KEY/OPENAI_BASE_URL/model settings.
The callback is a test HTTP service, not a full Bernstein orchestrator run.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from bernstein.core.models import AgentSession, ModelConfig

from bernstein.adapters.qwen import QwenAdapter
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.sandbox0 import Sandbox0SandboxBackend
from bernstein.core.sandbox.manifest import GitRepoEntry

pytestmark = pytest.mark.skipif(
    os.environ.get("CI_SANDBOX0_AGENT_TEST") != "1",
    reason="Set CI_SANDBOX0_AGENT_TEST=1 to create sandboxes and make real model calls",
)


def test_real_agent_commit_callback_and_sync_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    callback_url = os.environ["SANDBOX0_TEST_CALLBACK_URL"].rstrip("/")
    callback_port = int(os.environ.get("SANDBOX0_TEST_CALLBACK_PORT", "18052"))
    model = os.environ["SANDBOX0_AGENT_MODEL"]
    assert os.environ["OPENAI_API_KEY"]
    assert os.environ["OPENAI_BASE_URL"]
    token = secrets.token_urlsafe(32)
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            valid = self.path == "/complete" and hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            )
            self.send_response(200 if valid else 403)
            self.end_headers()
            if valid:
                completed.set()
            self.wfile.write(b"OK" if valid else b"Denied")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", callback_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", callback_url)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / ".gitignore").write_text(".sdd/\naudit.key\n__pycache__/\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    session_id = "sandbox0-agent-smoke"
    token_path = tmp_path / ".sdd" / "runtime" / "agent.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(token)
    token_path.chmod(0o600)
    backend = Sandbox0SandboxBackend()
    adapter = QwenAdapter()
    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=tmp_path,
        workdir=tmp_path,
        use_worktrees=False,
        sandbox_backend=backend,
        sandbox_manifest_factory=lambda: WorkspaceManifest(repo=GitRepoEntry(str(tmp_path), "main")),
    )
    spawner._agent_token_files[session_id] = token_path
    agent = AgentSession(id=session_id, role="backend", timeout_s=300)
    try:
        spawner._spawn_via_sandbox_session(
            session_id=session_id,
            prompt=(
                "Fix calculator.py: change the subtraction to exactly `return a + b`. "
                "Run python3 to assert add(2, 3) == 5 and add(-2, 2) == 0. "
                "Also assert that SANDBOX0_API_KEY is absent from os.environ. "
                "Commit only calculator.py using Git. Do not create a branch. "
                f"Then POST an empty body to {callback_url}/complete with urllib.request. "
                f"Read the bearer token from {token_path}; set the Authorization header. "
                "Do not print, persist, or commit any credential. Exit when all steps succeed."
            ),
            spawn_cwd=tmp_path,
            model_config=ModelConfig(model, "high"),
            mcp_config=None,
            session=agent,
            adapter=adapter,
        )
        handle = spawner._sandbox_exec_handles[session_id]
        result = handle.future.result(timeout=360)
        assert result.exit_code == 0, "Agent failed; inspect the private test log"
        assert completed.wait(5), "Agent did not authenticate to the remote callback"
        # Future completion precedes the sync-back/destroy callback.
        deadline = time.monotonic() + 150
        while backend._sessions and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not backend._sessions, "Agent sandbox was not reaped"
        ref = f"refs/remotes/sandbox/{session_id}/main"
        code = subprocess.check_output(["git", "show", f"{ref}:calculator.py"], cwd=tmp_path).decode()
        assert "return a + b" in code
        assert "return a - b" in (tmp_path / "calculator.py").read_text()
        tracked = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", ref], cwd=tmp_path).decode()
        assert set(tracked.splitlines()) == {".gitignore", "calculator.py"}
        assert (tmp_path / ".sdd" / "runtime" / "sandbox" / f"{session_id}.bundle").is_file()
    finally:
        asyncio.run(backend.destroy_all())
        backend.client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        token_path.unlink(missing_ok=True)
