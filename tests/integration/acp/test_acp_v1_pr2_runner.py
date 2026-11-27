"""Official ACP v1 SDK integration tests for PR 2: embedded runner, journal updates,
approval-gate permission bridge, ``session/load``, ``--setup``, ``auth_headers()``,
mid-turn ``session/cancel`` task/worker teardown, and ``stdin`` EOF response flushing.

All tests use deterministic local stubs/mock adapter (zero paid calls, zero API keys).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

acp = pytest.importorskip("acp")
from acp.exceptions import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AuthCapabilities,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)

from bernstein.core.replay.journal import contained_run_journal, load_events, verify_journal
from bernstein.core.run_auth_token import persist_run_auth_token
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

pytestmark = pytest.mark.integration


class _RecordingSdkClient:
    """Official ACP SDK client capturing session updates and answering permission RPCs."""

    def __init__(self, *, option_kind: str = "allow_once", deny: bool = False) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permission_requests: list[tuple[str, ToolCallUpdate, list[PermissionOption]]] = []
        self.option_kind = option_kind
        self.deny = deny
        self.tool_call_started = asyncio.Event()

    async def session_update(self, session_id: str, update: Any, **_kwargs: Any) -> None:
        self.updates.append((session_id, update))
        if isinstance(update, ToolCallStart):
            self.tool_call_started.set()

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **_kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((session_id, tool_call, options))
        if self.deny:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        matched = next((o for o in options if o.kind == self.option_kind), None)
        chosen = matched.option_id if matched is not None else (options[0].option_id if options else "allow_once")
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=chosen))


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["BERNSTEIN_DEFAULT_CLI"] = "mock"
    env.pop("BERNSTEIN_SERVER_URL", None)
    env.pop("BERNSTEIN_AUTH_TOKEN", None)
    if extra:
        env.update(extra)
    return env


def _init_demo_workspace(workspace: Path, *, interactive_approvals: bool = True) -> None:
    """Create a minimal git repo with a fixable bug and bernstein.yaml."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "app.py").write_text(
        "from flask import Flask, jsonify\n"
        'ITEMS = ["a", "b"]\n'
        "def get_item(n: int):\n"
        '    return jsonify({"id": n, "item": ITEMS[n]})  # off-by-one\n',
        encoding="utf-8",
    )
    (workspace / "bernstein.yaml").write_text(
        f"approvals:\n  interactive: {'true' if interactive_approvals else 'false'}\n  timeout_seconds: 15\n",
        encoding="utf-8",
    )
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@bernstein.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@bernstein.invalid",
    }
    subprocess.run(["git", "init", "-q"], cwd=workspace, env=git_env, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, env=git_env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=workspace, env=git_env, check=True)


async def _start_auth_checked_task_server(
    expected_token: str,
    *,
    create_delay_s: float = 0.0,
) -> tuple[asyncio.Server, str, list[dict[str, Any]]]:
    """HTTP/1.1 stub task server requiring ``Authorization: Bearer <expected_token>``."""
    seen: list[dict[str, Any]] = []
    seq = 0

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal seq
        try:
            req_line = await reader.readline()
            if not req_line:
                return
            parts = req_line.decode("ascii", errors="replace").split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            body_raw = await reader.readexactly(length) if length > 0 else b"{}"
            payload = json.loads(body_raw.decode("utf-8")) if body_raw else {}
            auth_val = headers.get("authorization", "")
            seen.append({"method": method, "path": path, "authorization": auth_val, "payload": payload})

            if auth_val != f"Bearer {expected_token}":
                err_body = b'{"detail":"unauthorized"}'
                writer.write(
                    (
                        f"HTTP/1.1 401 Unauthorized\r\n"
                        f"content-type: application/json\r\n"
                        f"content-length: {len(err_body)}\r\n"
                        f"connection: close\r\n\r\n"
                    ).encode("latin-1")
                    + err_body
                )
                await writer.drain()
                return

            if path == "/tasks" and create_delay_s > 0:
                await asyncio.sleep(create_delay_s)

            seq += 1
            resp_body = json.dumps({"id": f"task-auth-{seq}"}).encode("utf-8")
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\n"
                    f"content-type: application/json\r\n"
                    f"content-length: {len(resp_body)}\r\n"
                    f"connection: close\r\n\r\n"
                ).encode("latin-1")
                + resp_body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, f"http://{host}:{port}", seen


@pytest.mark.asyncio
async def test_sdk_subprocess_standalone_full_cycle_updates_permission_and_load(tmp_path: Path) -> None:
    """Full ACP v1 cycle on ``bernstein acp serve --stdio`` WITHOUT ``--server-url``."""
    workspace = tmp_path / "demo_ws"
    _init_demo_workspace(workspace, interactive_approvals=True)

    client1 = _RecordingSdkClient(option_kind="allow_once")
    async with acp.spawn_agent_process(
        client1,
        sys.executable,
        "-m",
        "bernstein",
        "acp",
        "serve",
        "--stdio",
        "--cli",
        "mock",
        env=_subprocess_env(),
    ) as (conn1, _proc1):
        init = await conn1.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(auth=AuthCapabilities(terminal=True)),
            client_info=Implementation(name="sdk-pr2-client", version="1.1.0"),
        )
        assert init.protocol_version == 1
        assert init.agent_capabilities is not None
        assert init.agent_capabilities.load_session is True

        sess = await conn1.new_session(cwd=str(workspace), mcp_servers=[])
        session_id = sess.session_id
        assert sess.modes is not None
        assert sess.modes.current_mode_id == "manual"

        prompt_resp = await conn1.prompt(
            session_id=session_id,
            prompt=[TextContentBlock(type="text", text="Fix off-by-one bug in app.py")],
        )
        assert prompt_resp.stop_reason == "end_turn"
        assert prompt_resp.usage is not None
        assert prompt_resp.usage.total_tokens >= 0

    assert len(client1.permission_requests) >= 1
    assert client1.permission_requests[0][0] == session_id

    updates = [u for sid, u in client1.updates if sid == session_id]
    assert any(isinstance(u, AgentPlanUpdate) and len(u.entries) >= 1 for u in updates), updates
    assert any(isinstance(u, AgentPlanUpdate) and any(e.status == "completed" for e in u.entries) for u in updates), (
        updates
    )
    assert any(isinstance(u, ToolCallStart) for u in updates), updates
    assert any(isinstance(u, ToolCallProgress) and u.status == "completed" for u in updates), updates
    assert any(isinstance(u, (AgentMessageChunk, AgentThoughtChunk)) for u in updates), updates
    assert any(isinstance(u, UsageUpdate) and u.size > 0 for u in updates), updates

    assert "ITEMS[n - 1]" in (workspace / "app.py").read_text(encoding="utf-8")

    # TaskStore persisted status == DONE on disk.
    store = TaskStore(workspace / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    assert len(store._tasks) == 1
    assert next(iter(store._tasks.values())).status == TaskStatus.DONE

    # Canonical EventJournal on disk passes Merkle verification.
    runs_root = workspace / ".sdd" / "runs"
    journals = [
        p
        for d in sorted(runs_root.iterdir())
        if d.is_dir() and (p := contained_run_journal(runs_root, d.name)) is not None and p.exists()
    ]
    assert journals, "Expected at least one EventJournal under .sdd/runs/"
    verdict = verify_journal(journals[0])
    assert verdict.chain_consistent is True
    assert verdict.count >= 4

    # Fresh subprocess reloads the session from disk via session/load.
    client2 = _RecordingSdkClient()
    async with acp.spawn_agent_process(
        client2,
        sys.executable,
        "-m",
        "bernstein",
        "acp",
        "serve",
        "--stdio",
        "--cli",
        "mock",
        env=_subprocess_env(),
    ) as (conn2, _proc2):
        await conn2.initialize(protocol_version=1)

        with pytest.raises(RequestError) as missing_exc:
            await conn2.load_session(cwd=str(workspace), mcp_servers=[], session_id="sess-does-not-exist")
        assert missing_exc.value.code == -32002

        loaded = await conn2.load_session(cwd=str(workspace), mcp_servers=[], session_id=session_id)
        assert loaded.modes is not None
        assert loaded.modes.current_mode_id == "manual"

    replayed = [u for sid, u in client2.updates if sid == session_id]
    assert any(isinstance(u, UserMessageChunk) for u in replayed), replayed
    assert any(isinstance(u, AgentPlanUpdate) for u in replayed), replayed
    assert any(isinstance(u, ToolCallStart) for u in replayed), replayed
    assert any(isinstance(u, AgentMessageChunk) for u in replayed), replayed
    assert any(isinstance(u, UsageUpdate) for u in replayed), replayed


@pytest.mark.asyncio
async def test_sdk_subprocess_midturn_cancel_really_cancels_task_and_worker(tmp_path: Path) -> None:
    """Mid-turn ``session/cancel`` kills the running worker and marks the task ``cancelled`` in TaskStore."""
    workspace = tmp_path / "cancel_ws"
    _init_demo_workspace(workspace, interactive_approvals=False)

    client = _RecordingSdkClient()
    idle_env = _subprocess_env(
        {
            "BERNSTEIN_MOCK_IDLE": "1",
            "BERNSTEIN_MOCK_IDLE_MIN_S": "30",
            "BERNSTEIN_MOCK_IDLE_MAX_S": "30",
        }
    )
    async with acp.spawn_agent_process(
        client,
        sys.executable,
        "-m",
        "bernstein",
        "acp",
        "serve",
        "--stdio",
        "--cli",
        "mock",
        env=idle_env,
    ) as (conn, _proc):
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(workspace), mcp_servers=[])
        session_id = sess.session_id
        await conn.set_session_mode(session_id=session_id, mode_id="auto")

        prompt_task = asyncio.create_task(
            conn.prompt(
                session_id=session_id,
                prompt=[TextContentBlock(type="text", text="Long running task to cancel")],
            )
        )
        await asyncio.wait_for(client.tool_call_started.wait(), timeout=5.0)
        # Allow MockAgentAdapter subprocess to spawn and record worker_spawned in journal.
        runs_root = workspace / ".sdd" / "runs"
        spawned_pid: int | None = None
        for _ in range(40):
            if runs_root.is_dir():
                for d in runs_root.iterdir():
                    jpath = contained_run_journal(runs_root, d.name) if d.is_dir() else None
                    if jpath and jpath.is_file():
                        for ev in load_events(jpath).events:
                            if ev.get("event") == "worker_spawned":
                                spawned_pid = int(ev["pid"])
                                break
            if spawned_pid is not None:
                break
            await asyncio.sleep(0.05)

        assert spawned_pid is not None, "Expected worker_spawned event with child PID before cancel"
        await conn.cancel(session_id=session_id)
        cancel_resp = await asyncio.wait_for(prompt_task, timeout=5.0)
        assert cancel_resp.stop_reason == "cancelled"

    # 1. Worker subprocess must be genuinely dead (not left running in background).
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pid, 0)

    # 2. Task in TaskStore (.sdd/runtime/tasks.jsonl) must have status == CANCELLED.
    store = TaskStore(workspace / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    assert len(store._tasks) == 1
    cancelled_task = next(iter(store._tasks.values()))
    assert cancelled_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_sdk_subprocess_permission_denial_blocks_execution(tmp_path: Path) -> None:
    """Denying ``session/request_permission`` blocks worker mutation via the approval gate."""
    workspace = tmp_path / "denied_ws"
    _init_demo_workspace(workspace, interactive_approvals=True)
    original_code = (workspace / "app.py").read_text(encoding="utf-8")

    client = _RecordingSdkClient(deny=True)
    async with acp.spawn_agent_process(
        client,
        sys.executable,
        "-m",
        "bernstein",
        "acp",
        "serve",
        "--stdio",
        "--cli",
        "mock",
        env=_subprocess_env(),
    ) as (conn, _proc):
        await conn.initialize(protocol_version=1)
        sess = await conn.new_session(cwd=str(workspace), mcp_servers=[])
        resp = await conn.prompt(
            session_id=sess.session_id,
            prompt=[TextContentBlock(type="text", text="Fix off-by-one bug in app.py")],
        )
        assert resp.stop_reason in {"end_turn", "refusal"}

    assert len(client.permission_requests) == 1
    assert (workspace / "app.py").read_text(encoding="utf-8") == original_code
    assert any(isinstance(u, ToolCallProgress) and u.status == "failed" for _, u in client.updates)


@pytest.mark.asyncio
async def test_cli_terminal_auth_setup_matches_advertised_auth_methods(tmp_path: Path) -> None:
    """``authMethods`` terminal ``args`` advertised by ``initialize`` execute cleanly on the CLI."""
    client = _RecordingSdkClient()
    base_cmd = [sys.executable, "-m", "bernstein", "acp", "serve", "--stdio"]
    async with acp.spawn_agent_process(
        client,
        *base_cmd,
        env=_subprocess_env(),
    ) as (conn, _proc):
        init = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(auth=AuthCapabilities(terminal=True)),
        )
    terminal_methods = [m for m in (init.auth_methods or []) if getattr(m, "type", None) == "terminal"]
    assert terminal_methods, f"Expected terminal auth method in {init.auth_methods}"
    advertised_args = list(getattr(terminal_methods[0], "args", None) or [])
    assert advertised_args == ["--setup"]

    ok_proc = subprocess.run(
        [*base_cmd, *advertised_args, "--cli", "mock"],
        cwd=tmp_path,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert ok_proc.returncode == 0, ok_proc.stderr + ok_proc.stdout

    isolated_env = _subprocess_env({"PATH": str(tmp_path), "BERNSTEIN_DEFAULT_CLI": ""})
    fail_proc = subprocess.run(
        [*base_cmd, *advertised_args],
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert fail_proc.returncode != 0
    combined = (fail_proc.stdout + fail_proc.stderr).lower()
    assert "adapter" in combined or "connect" in combined


@pytest.mark.asyncio
async def test_sdk_subprocess_server_url_includes_auth_headers_and_cancels_inflight_task(tmp_path: Path) -> None:
    """``--server-url`` attaches ``auth_headers()`` and cancels the real task ID even under in-flight POST race."""
    workspace = tmp_path / "remote_ws"
    workspace.mkdir(parents=True, exist_ok=True)
    secret_token = "acp-pr2-bearer-token-9f8e7d"
    persist_run_auth_token(workspace, secret_token)

    server, server_url, seen = await _start_auth_checked_task_server(secret_token, create_delay_s=0.25)
    client = _RecordingSdkClient()
    try:
        async with acp.spawn_agent_process(
            client,
            sys.executable,
            "-m",
            "bernstein",
            "acp",
            "serve",
            "--stdio",
            "--server-url",
            server_url,
            env=_subprocess_env(),
        ) as (conn, _proc):
            await conn.initialize(protocol_version=1)
            sess = await conn.new_session(cwd=str(workspace), mcp_servers=[])
            prompt_task = asyncio.create_task(
                conn.prompt(
                    session_id=sess.session_id,
                    prompt=[TextContentBlock(type="text", text="Run remote task with bearer token")],
                )
            )
            # Send session/cancel while POST /tasks (0.25s) is still in flight (B1 race).
            await asyncio.sleep(0.05)
            await conn.cancel(session_id=sess.session_id)
            resp = await asyncio.wait_for(prompt_task, timeout=5.0)
            assert resp.stop_reason == "cancelled"
    finally:
        server.close()
        await server.wait_closed()

    assert any(r["path"] == "/tasks" and r["authorization"] == f"Bearer {secret_token}" for r in seen), seen
    assert any(
        r["path"] == "/tasks/task-auth-1/cancel" and r["authorization"] == f"Bearer {secret_token}" for r in seen
    ), seen


def test_stdio_eof_preserves_inflight_prompt_response(tmp_path: Path) -> None:
    """Piped stdin EOF immediately after ``session/prompt`` flushes the prompt response and settles cleanly (S1/S2)."""
    workspace = tmp_path / "eof_ws"
    _init_demo_workspace(workspace, interactive_approvals=False)

    proc = subprocess.Popen(
        [sys.executable, "-m", "bernstein", "acp", "serve", "--stdio", "--cli", "mock"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(),
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n"
    )
    proc.stdin.flush()
    init_line = proc.stdout.readline()
    assert json.loads(init_line)["id"] == 1

    proc.stdin.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(workspace), "mcpServers": []}}
        )
        + "\n"
    )
    proc.stdin.flush()
    sess_resp = json.loads(proc.stdout.readline())
    sid = sess_resp["result"]["sessionId"]

    proc.stdin.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "session/set_mode", "params": {"sessionId": sid, "modeId": "auto"}}
        )
        + "\n"
    )
    proc.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "Fix off-by-one bug in app.py"}]},
            }
        )
        + "\n"
    )
    proc.stdin.flush()
    proc.stdin.close()
    proc.stdin = None
    remaining_stdout, _ = proc.communicate(timeout=15)
    frames = [json.loads(ln) for ln in remaining_stdout.splitlines() if ln.strip()]
    assert any(f.get("id") == 4 and "result" in f for f in frames), frames
