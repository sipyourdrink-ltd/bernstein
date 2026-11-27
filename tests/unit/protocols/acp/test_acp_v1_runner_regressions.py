"""Unit regression tests for ACP v1 embedded runner, session/load isolation,
approval-gate RPC cleanup, terminal setup discovery, and journal projection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bernstein.adapters import registry as adapter_registry
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.cli.commands.acp_cmd import run_terminal_setup
from bernstein.core.config.platform_compat import reap_process_group
from bernstein.core.protocols.acp import (
    AuthHandler,
    PromptRunner,
    SessionLoader,
)
from bernstein.core.protocols.acp.handlers import (
    ACPRequestContext,
    AuditEmitter,
    ClientRequester,
    PermissionAsker,
    StreamPublisher,
    TaskCanceller,
    TaskCreator,
)
from bernstein.core.protocols.acp.projection import project_journal_to_acp_updates
from bernstein.core.protocols.acp.runner import (
    EmbeddedOrchestratorRunner,
    _ActiveWorker,  # pyright: ignore[reportPrivateUsage]
    _reap_worker,  # pyright: ignore[reportPrivateUsage]
)
from bernstein.core.protocols.acp.schema import (
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    RESOURCE_NOT_FOUND,
    ACPSchemaError,
    validate_request,
)
from bernstein.core.protocols.acp.server import build_default_server
from bernstein.core.protocols.acp.session import (
    _SAFE_ID_RE,  # pyright: ignore[reportPrivateUsage]
    ACPSession,
    load_persisted_session,
)
from bernstein.core.protocols.acp.transport import StdioAcpTransport, dispatch_frame
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.tasks.models import ModelConfig, TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _wire_stdio(server: Any) -> tuple[StdioAcpTransport, asyncio.StreamReader, asyncio.StreamReader]:
    in_reader = asyncio.StreamReader()
    out_capture = asyncio.StreamReader()

    class _MemWriter:
        def write(self, data: bytes) -> None:
            out_capture.feed_data(data)

        async def drain(self) -> None:
            await asyncio.sleep(0)

        def close(self) -> None:
            out_capture.feed_eof()

    transport = StdioAcpTransport(
        registry=server.registry,
        reader=in_reader,
        writer=_MemWriter(),  # type: ignore[arg-type]
    )
    return transport, in_reader, out_capture


def test_real_cli_adapter_cancel_reaps_grandchild_and_spawn_is_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real CLI adapter spawn must not stall the event loop and cancel must reap grandchildren."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gc_gemini = tmp_path / "gc_gemini.txt"
    gc_claude = tmp_path / "gc_claude.txt"
    _write_executable(
        bin_dir / "gemini",
        f"#!/bin/sh\nsleep 30 &\necho $! > {gc_gemini}\nwait\n",
    )
    _write_executable(
        bin_dir / "claude",
        f"#!/bin/sh\nsleep 30 &\necho $! > {gc_claude}\nwait\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    for cli_name, gc_file in (("gemini", gc_gemini), ("claude", gc_claude)):
        ws = tmp_path / f"ws_{cli_name}"
        ws.mkdir()
        (ws / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")

        server = build_default_server(server_url=None, adapter_name=cli_name)
        gc_pid: int | None = None

        async def _run(
            srv: Any = server,
            workdir: Path = ws,
            pid_file: Path = gc_file,
            cname: str = cli_name,
        ) -> None:
            nonlocal gc_pid
            await srv.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
            sess = await srv.registry.dispatch(
                ACPRequestContext("session/new", 2),
                {"cwd": str(workdir), "mcpServers": []},
            )
            sid = sess["sessionId"]
            await srv.registry.dispatch(ACPRequestContext("session/set_mode", 3), {"sessionId": sid, "modeId": "auto"})

            prompt_task = asyncio.create_task(
                srv.registry.dispatch(
                    ACPRequestContext("session/prompt", 4),
                    {"sessionId": sid, "prompt": [{"type": "text", "text": f"Run {cname} shim"}]},
                )
            )

            t0 = time.monotonic()
            for _ in range(40):
                if pid_file.is_file() and pid_file.read_text(encoding="utf-8").strip():
                    gc_pid = int(pid_file.read_text(encoding="utf-8").strip())
                    break
                await asyncio.sleep(0.05)
            elapsed_to_observe = time.monotonic() - t0
            assert elapsed_to_observe < 2.0, f"Event loop blocked during {cname}.spawn(): {elapsed_to_observe:.2f}s"
            assert gc_pid is not None and _pid_alive(gc_pid)

            await srv.registry.dispatch(ACPRequestContext("session/cancel", None), {"sessionId": sid})
            resp = await asyncio.wait_for(prompt_task, timeout=3.0)
            assert resp["stopReason"] == "cancelled"

        try:
            asyncio.run(_run())
            assert gc_pid is not None
            assert not _pid_alive(gc_pid), f"{cli_name} grandchild {gc_pid} survived mid-turn session/cancel"

            store = TaskStore(ws / ".sdd" / "runtime" / "tasks.jsonl")
            store.replay_jsonl()
            assert store.list_tasks()[0].status == TaskStatus.CANCELLED
        finally:
            if gc_pid is not None and _pid_alive(gc_pid):
                with contextlib.suppress(OSError):
                    os.kill(gc_pid, 9)


def test_session_load_rejects_foreign_non_acp_cli_run(tmp_path: Path) -> None:
    """session/load must reject non-ACP CLI journals lacking persisted ACP session metadata."""
    ws = tmp_path / "ws_foreign"
    ws.mkdir()
    foreign_journal = EventJournal("cli-run-foreign", ws / ".sdd")
    foreign_journal.record("agent_message", text="NON_ACP_CLI_RUN_PAYLOAD")

    frames: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    server = build_default_server(server_url=None, adapter_name="mock", stream_publisher=_publish)

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as exc_info:
            await server.registry.dispatch(
                ACPRequestContext("session/load", 2),
                {"sessionId": "cli-run-foreign", "cwd": str(ws), "mcpServers": []},
            )
        assert exc_info.value.code == RESOURCE_NOT_FOUND
        assert "NON_ACP_CLI_RUN_PAYLOAD" not in json.dumps(frames)

    asyncio.run(_run())


def test_session_load_rejects_cross_workspace_in_memory_rebinding(tmp_path: Path) -> None:
    """session/load must not rebind an in-memory session created for ws_a onto ws_b."""
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()

    server = build_default_server(server_url=None, adapter_name="mock")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        created = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(ws_a), "mcpServers": []},
        )
        sid_a = created["sessionId"]
        with pytest.raises(ACPSchemaError) as exc_info:
            await server.registry.dispatch(
                ACPRequestContext("session/load", 3),
                {"sessionId": sid_a, "cwd": str(ws_b), "mcpServers": []},
            )
        assert exc_info.value.code == RESOURCE_NOT_FOUND
        sess_obj = await server.registry.sessions.get(sid_a)
        assert sess_obj is not None
        assert Path(sess_obj.cwd).resolve() == ws_a.resolve()

    asyncio.run(_run())


def test_session_load_rejects_tampered_persisted_metadata(tmp_path: Path) -> None:
    """session/load must reject persisted metadata with mismatched session_id, cwd, or run_id."""
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    foreign = EventJournal("cli-run-other", ws_a / ".sdd")
    foreign.record("agent_message", text="TAMPERED_TARGET_RUN")

    acp_dir = ws_a / ".sdd" / "runtime" / "acp_sessions"
    acp_dir.mkdir(parents=True, exist_ok=True)
    (acp_dir / "sess-tamper-id.json").write_text(
        json.dumps({"session_id": "sess-OTHER", "cwd": str(ws_a), "run_id": "sess-tamper-id", "mode": "auto"}),
        encoding="utf-8",
    )
    (acp_dir / "sess-tamper-cwd.json").write_text(
        json.dumps({"session_id": "sess-tamper-cwd", "cwd": str(ws_b), "run_id": "sess-tamper-cwd", "mode": "auto"}),
        encoding="utf-8",
    )
    (acp_dir / "sess-tamper-run.json").write_text(
        json.dumps({"session_id": "sess-tamper-run", "cwd": str(ws_a), "run_id": "cli-run-other", "mode": "auto"}),
        encoding="utf-8",
    )

    server = build_default_server(server_url=None, adapter_name="mock")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        for bad_sid in ("sess-tamper-id", "sess-tamper-cwd", "sess-tamper-run"):
            with pytest.raises(ACPSchemaError) as exc_info:
                await server.registry.dispatch(
                    ACPRequestContext("session/load", 2),
                    {"sessionId": bad_sid, "cwd": str(ws_a), "mcpServers": []},
                )
            assert exc_info.value.code == RESOURCE_NOT_FOUND

    asyncio.run(_run())


def test_session_load_rejects_session_with_active_turn(tmp_path: Path) -> None:
    """session/load must reject reloading a session while a prompt turn is active."""
    ws = tmp_path / "ws_active"
    ws.mkdir()
    (ws / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_worker(_sess: Any, _prompt: str, _wd: Path, _cancel_ev: asyncio.Event) -> str:
        started.set()
        await release.wait()
        return "done"

    server = build_default_server(server_url=None, adapter_name="mock")
    runner = EmbeddedOrchestratorRunner(registry=server.registry, worker_executor=_slow_worker)
    server.registry.prompt_runner = runner.run_prompt

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        created = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(ws), "mcpServers": []},
        )
        sid = created["sessionId"]
        await server.registry.dispatch(ACPRequestContext("session/set_mode", 3), {"sessionId": sid, "modeId": "auto"})
        turn = asyncio.create_task(
            server.registry.dispatch(
                ACPRequestContext("session/prompt", 4),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "Hold turn open"}]},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        with pytest.raises(ACPSchemaError) as exc_info:
            await server.registry.dispatch(
                ACPRequestContext("session/load", 5),
                {"sessionId": sid, "cwd": str(ws), "mcpServers": []},
            )
        assert exc_info.value.code == INVALID_REQUEST
        release.set()
        await asyncio.wait_for(turn, timeout=2.0)

    asyncio.run(_run())


def test_approval_timeout_cleans_pending_outbound_permission_rpc(tmp_path: Path) -> None:
    """ApprovalTimeoutError must cancel the outbound permission RPC and clear _pending_outbound."""
    (tmp_path / "bernstein.yaml").write_text(
        "approvals:\n  interactive: true\n  timeout_seconds: 1\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    server = build_default_server(server_url=None, adapter_name="mock")
    server.registry.permission_timeout_s = 60.0

    async def _run() -> None:
        transport, in_reader, out_capture = _wire_stdio(server)
        serve_task = asyncio.create_task(transport.serve_forever())
        try:
            in_reader.feed_data(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
                    + "\n"
                ).encode("utf-8")
            )
            _ = await asyncio.wait_for(out_capture.readline(), timeout=2.0)

            in_reader.feed_data(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/new",
                            "params": {"cwd": str(tmp_path), "mcpServers": []},
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            sess_line = await asyncio.wait_for(out_capture.readline(), timeout=2.0)
            sid = json.loads(sess_line)["result"]["sessionId"]

            in_reader.feed_data(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "session/prompt",
                            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "Edit app.py"}]},
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )

            saw_perm_req = False
            pending_outbound: dict[Any, Any] = transport._pending_outbound  # pyright: ignore[reportPrivateUsage]
            while True:
                raw = await asyncio.wait_for(out_capture.readline(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("method") == "session/request_permission":
                    saw_perm_req = True
                    assert len(pending_outbound) == 1
                if msg.get("id") == 3:
                    assert msg["result"]["stopReason"] == "end_turn"
                    assert len(pending_outbound) == 0
                    break

            assert saw_perm_req
            assert len(pending_outbound) == 0
        finally:
            in_reader.feed_eof()
            await asyncio.wait_for(serve_task, timeout=2.0)

    asyncio.run(_run())


def test_worker_fast_exit_marks_task_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast-exiting CLI adapter raising SpawnError must mark the TaskStore entry FAILED."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "gemini", "#!/bin/sh\necho 'fatal startup error' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    ws = tmp_path / "ws_fast"
    ws.mkdir()
    (ws / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")

    server = build_default_server(server_url=None, adapter_name="gemini")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        sess = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(ws), "mcpServers": []},
        )
        await server.registry.dispatch(
            ACPRequestContext("session/set_mode", 3),
            {"sessionId": sess["sessionId"], "modeId": "auto"},
        )
        with pytest.raises(ACPSchemaError) as exc_info:
            await server.registry.dispatch(
                ACPRequestContext("session/prompt", 4),
                {"sessionId": sess["sessionId"], "prompt": [{"type": "text", "text": "Fail fast"}]},
            )
        assert exc_info.value.code == INTERNAL_ERROR

    asyncio.run(_run())
    store = TaskStore(ws / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    assert store.list_tasks()[0].status == TaskStatus.FAILED


def test_worker_executor_exception_marks_task_failed(tmp_path: Path) -> None:
    """An exception from worker_executor must settle the TaskStore entry as FAILED."""
    ws = tmp_path / "ws_exc"
    ws.mkdir()
    (ws / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")

    async def _exploding_worker(_sess: Any, _prompt: str, _wd: Path, _cancel_ev: asyncio.Event) -> str:
        raise RuntimeError("worker crashed")

    server = build_default_server(server_url=None, adapter_name="mock")
    runner = EmbeddedOrchestratorRunner(registry=server.registry, worker_executor=_exploding_worker)
    server.registry.prompt_runner = runner.run_prompt

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        sess = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(ws), "mcpServers": []},
        )
        await server.registry.dispatch(
            ACPRequestContext("session/set_mode", 3),
            {"sessionId": sess["sessionId"], "modeId": "auto"},
        )
        with pytest.raises(ACPSchemaError) as exc_info:
            await server.registry.dispatch(
                ACPRequestContext("session/prompt", 4),
                {"sessionId": sess["sessionId"], "prompt": [{"type": "text", "text": "Crash worker"}]},
            )
        assert exc_info.value.code == INTERNAL_ERROR

    asyncio.run(_run())
    store = TaskStore(ws / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    assert store.list_tasks()[0].status == TaskStatus.FAILED


def test_worker_late_nonzero_exit_marks_task_failed_and_emits_failed_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker exiting non-zero after spawn must mark TaskStore FAILED and emit failed tool_call_update."""
    ws = tmp_path / "ws_late"
    ws.mkdir()
    (ws / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")
    updates: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        updates.append(frame)

    class _LateFailAdapter:
        default_model = "mock"

        def spawn(self, *, workdir: Path, session_id: str, **_kw: Any) -> SpawnResult:
            log_dir = workdir / ".sdd" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{session_id}.log"
            log_path.write_text("unit test failed: exit 1\n", encoding="utf-8")
            proc = subprocess.Popen(["/bin/sh", "-c", "sleep 0.05; exit 1"])
            return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

        def kill(self, pid: int) -> object:
            return reap_process_group(pid)

    def _get_late_adapter(_name: str) -> Any:
        return _LateFailAdapter()

    monkeypatch.setattr("bernstein.adapters.registry.get_adapter", _get_late_adapter)
    server = build_default_server(server_url=None, adapter_name="mock", stream_publisher=_publish)

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        sess = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(ws), "mcpServers": []},
        )
        await server.registry.dispatch(
            ACPRequestContext("session/set_mode", 3),
            {"sessionId": sess["sessionId"], "modeId": "auto"},
        )
        res = await server.registry.dispatch(
            ACPRequestContext("session/prompt", 4),
            {"sessionId": sess["sessionId"], "prompt": [{"type": "text", "text": "Fail late"}]},
        )
        assert res["stopReason"] == "end_turn"

    asyncio.run(_run())
    store = TaskStore(ws / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    assert store.list_tasks()[0].status == TaskStatus.FAILED
    tc_statuses = [
        f["params"]["update"]["status"]
        for f in updates
        if f.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call_update"
    ]
    assert "failed" in tc_statuses


def test_terminal_setup_validates_all_selectable_adapter_binaries(tmp_path: Path) -> None:
    """--setup --cli <adapter> validates the adapter's real binary across selectable adapters."""
    _write_executable(tmp_path / "opencode", "#!/bin/sh\necho 0.1.0\n")
    _write_executable(tmp_path / "q", "#!/bin/sh\necho 1.0.0\n")
    assert run_terminal_setup(adapter_name="opencode", env={"PATH": str(tmp_path)}) == 0
    assert run_terminal_setup(adapter_name="q_dev", env={"PATH": str(tmp_path)}) == 0
    assert run_terminal_setup(adapter_name=None, env={"PATH": str(tmp_path)}) == 0
    assert run_terminal_setup(adapter_name="goose", env={"PATH": str(tmp_path)}) == 1


def test_binary_prompt_target_and_non_utf8_worker_log_do_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary target files in prompt and non-UTF-8 worker logs must not crash with UnicodeDecodeError."""
    (tmp_path / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\xfd")

    class _BinaryLogAdapter:
        default_model = "mock"

        def spawn(self, *, workdir: Path, session_id: str, **_kw: Any) -> SpawnResult:
            log_dir = workdir / ".sdd" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{session_id}.log"
            log_path.write_bytes(b"\xff\xfecompleted binary check\n")
            proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
            return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

        def kill(self, pid: int) -> object:
            return reap_process_group(pid)

    def _get_binary_adapter(_name: str) -> Any:
        return _BinaryLogAdapter()

    monkeypatch.setattr("bernstein.adapters.registry.get_adapter", _get_binary_adapter)
    server = build_default_server(server_url=None, adapter_name="mock")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        sess = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        sid = sess["sessionId"]
        await server.registry.dispatch(ACPRequestContext("session/set_mode", 3), {"sessionId": sid, "modeId": "auto"})
        res = await server.registry.dispatch(
            ACPRequestContext("session/prompt", 4),
            {"sessionId": sid, "prompt": [{"type": "text", "text": "Inspect logo.png"}]},
        )
        assert res["stopReason"] == "end_turn"

    asyncio.run(_run())
    store = TaskStore(tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    assert store.list_tasks()[0].status == TaskStatus.DONE


def test_default_server_authenticates_advertised_terminal_setup_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_default_server() wires a default auth_handler for advertised 'bernstein-setup'."""
    server_ok = build_default_server(server_url=None, adapter_name="mock")

    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("BERNSTEIN_DEFAULT_CLI", "")
    server_missing = build_default_server(server_url=None, adapter_name=None)

    async def _run() -> None:
        init_ok = await server_ok.registry.dispatch(
            ACPRequestContext("initialize", 1),
            {"protocolVersion": 1, "clientCapabilities": {"auth": {"terminal": True}}},
        )
        assert init_ok["authMethods"][0]["id"] == "bernstein-setup"
        auth_res = await server_ok.registry.dispatch(
            ACPRequestContext("authenticate", 2),
            {"methodId": "bernstein-setup"},
        )
        assert auth_res == {}

        await server_missing.registry.dispatch(
            ACPRequestContext("initialize", 1),
            {"protocolVersion": 1, "clientCapabilities": {"auth": {"terminal": True}}},
        )
        with pytest.raises(ACPSchemaError) as fail_auth:
            await server_missing.registry.dispatch(
                ACPRequestContext("authenticate", 2),
                {"methodId": "bernstein-setup"},
            )
        assert fail_auth.value.code == AUTH_REQUIRED

    asyncio.run(_run())


def test_exported_handler_callback_aliases_are_runtime_type_aliases() -> None:
    """Exported handler callback type aliases must be runtime TypeAliasType objects, not plain str."""
    aliases = (
        PromptRunner,
        AuthHandler,
        TaskCreator,
        TaskCanceller,
        SessionLoader,
        StreamPublisher,
        ClientRequester,
        PermissionAsker,
        AuditEmitter,
    )
    for alias in aliases:
        assert not isinstance(alias, str), f"Expected type alias, got str: {alias!r}"


def test_projector_tolerates_non_numeric_and_non_finite_usage_fields(tmp_path: Path) -> None:
    """Malformed non-numeric or non-finite token/cost fields in journal.jsonl must not crash projection."""
    journal = EventJournal("run-bad-usage", tmp_path / ".sdd")
    journal.record(
        "usage_update",
        input_tokens="n/a",
        output_tokens=float("nan"),
        thought_tokens=None,
        used=float("inf"),
        size="invalid",
        cost_usd="not-a-float",
    )
    updates = project_journal_to_acp_updates(journal.path)
    assert any(
        u["sessionUpdate"] == "usage_update" and u["used"] == 0 and u["size"] > 0 and "cost" not in u for u in updates
    )


def test_stdin_eof_during_permission_wait_cancels_task_with_eof_reason(tmp_path: Path) -> None:
    """stdin EOF while awaiting session/request_permission must settle task as CANCELLED with transport_eof."""
    (tmp_path / "bernstein.yaml").write_text(
        "approvals:\n  interactive: true\n  timeout_seconds: 30\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    server = build_default_server(server_url=None, adapter_name="mock")
    sid = ""

    async def _run() -> None:
        nonlocal sid
        transport, in_reader, out_capture = _wire_stdio(server)
        serve_task = asyncio.create_task(transport.serve_forever())
        in_reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n"
            ).encode("utf-8")
        )
        _ = await asyncio.wait_for(out_capture.readline(), timeout=2.0)

        in_reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {"cwd": str(tmp_path), "mcpServers": []},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        sid = json.loads(await asyncio.wait_for(out_capture.readline(), timeout=2.0))["result"]["sessionId"]

        in_reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "Edit app.py"}]},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )

        while True:
            raw = await asyncio.wait_for(out_capture.readline(), timeout=3.0)
            msg = json.loads(raw)
            if msg.get("method") == "session/request_permission":
                break

        in_reader.feed_eof()
        await asyncio.wait_for(serve_task, timeout=3.0)

    asyncio.run(_run())
    store = TaskStore(tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    settled_task = store.list_tasks()[0]
    assert settled_task.status == TaskStatus.CANCELLED
    journal_path = tmp_path / ".sdd" / "runs" / sid / "journal.jsonl"
    cancelled_events = [e for e in load_events(journal_path).events if e.get("event") == "task_cancelled"]
    assert cancelled_events and cancelled_events[-1].get("reason") == "transport_eof"


def test_concurrent_turns_sharing_singleton_cli_adapter_do_not_cross_kill_or_mutate_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent turns sharing a singleton CLIAdapter must not mutate the instance or cross-kill."""
    entered_lock = threading.Lock()
    entered_sessions: list[str] = []
    mutated_probe_attr = False

    class _SingletonProbeAdapter(CLIAdapter):
        default_model = "singleton-mock"

        def name(self) -> str:
            return "Singleton Probe"

        def spawn(
            self,
            *,
            prompt: str,
            workdir: Path,
            model_config: ModelConfig,
            session_id: str,
            mcp_config: dict[str, Any] | None = None,
            timeout_seconds: int = 120,
            task_scope: str = "medium",
            budget_multiplier: float = 1.0,
            system_addendum: str = "",
            multimodal_context: Any | None = None,
        ) -> SpawnResult:
            nonlocal mutated_probe_attr
            del prompt, model_config, mcp_config, timeout_seconds, task_scope, budget_multiplier
            del system_addendum, multimodal_context
            if "_probe_fast_exit" in self.__dict__:
                mutated_probe_attr = True
            log_dir = workdir / ".sdd" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{session_id}.log"
            log_path.write_text("singleton turn completed\n", encoding="utf-8")
            proc = subprocess.Popen(["/bin/sh", "-c", "sleep 0.45"], start_new_session=True)
            with entered_lock:
                entered_sessions.append(session_id)
            self._probe_fast_exit(proc, log_path, provider_name="shared", timeout_seconds=1.5)
            return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    singleton = _SingletonProbeAdapter()
    monkeypatch.setitem(adapter_registry._ADAPTERS, "_probe_singleton", singleton)  # pyright: ignore[reportPrivateUsage]

    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    (ws1 / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")
    (ws2 / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")

    server = build_default_server(server_url=None, adapter_name="_probe_singleton")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        s1 = await server.registry.dispatch(ACPRequestContext("session/new", 2), {"cwd": str(ws1), "mcpServers": []})
        s2 = await server.registry.dispatch(ACPRequestContext("session/new", 3), {"cwd": str(ws2), "mcpServers": []})
        sid1 = s1["sessionId"]
        sid2 = s2["sessionId"]
        await server.registry.dispatch(ACPRequestContext("session/set_mode", 4), {"sessionId": sid1, "modeId": "auto"})
        await server.registry.dispatch(ACPRequestContext("session/set_mode", 5), {"sessionId": sid2, "modeId": "auto"})

        t1 = asyncio.create_task(
            server.registry.dispatch(
                ACPRequestContext("session/prompt", 6),
                {"sessionId": sid1, "prompt": [{"type": "text", "text": "Turn 1"}]},
            )
        )
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(
            server.registry.dispatch(
                ACPRequestContext("session/prompt", 7),
                {"sessionId": sid2, "prompt": [{"type": "text", "text": "Turn 2"}]},
            )
        )

        for _ in range(40):
            with entered_lock:
                count = len(entered_sessions)
            if count >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(entered_sessions) == 2
        assert "_probe_fast_exit" not in singleton.__dict__

        await server.registry.dispatch(ACPRequestContext("session/cancel", None), {"sessionId": sid1})
        r1 = await asyncio.wait_for(t1, timeout=3.0)
        r2 = await asyncio.wait_for(t2, timeout=3.0)
        assert r1["stopReason"] == "cancelled"
        assert r2["stopReason"] == "end_turn"

    asyncio.run(_run())
    assert not mutated_probe_attr
    assert "_probe_fast_exit" not in singleton.__dict__

    st1 = TaskStore(ws1 / ".sdd" / "runtime" / "tasks.jsonl")
    st1.replay_jsonl()
    assert st1.list_tasks()[0].status == TaskStatus.CANCELLED

    st2 = TaskStore(ws2 / ".sdd" / "runtime" / "tasks.jsonl")
    st2.replay_jsonl()
    assert st2.list_tasks()[0].status == TaskStatus.DONE


def test_reap_worker_reaps_sigterm_group_without_zombie_stall_and_deduplicates_concurrent_reaps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_reap_worker must reap SIGTERM-responsive children without a 3s zombie wait and deduplicate reaps."""
    caplog.set_level(logging.WARNING)
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"], start_new_session=True)
    worker = _ActiveWorker(adapter=GeminiAdapter(), proc=proc, pid=proc.pid)
    try:
        t0 = time.monotonic()
        _reap_worker(worker)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.8, f"_reap_worker stalled on zombie leader: {elapsed:.3f}s"
        assert proc.returncode == -signal.SIGTERM
        assert not _pid_alive(proc.pid)
        assert "did not exit within" not in caplog.text
    finally:
        if _pid_alive(proc.pid):
            reap_process_group(proc.pid, grace_seconds=0.1)

    kill_calls = 0
    kill_lock = threading.Lock()

    class _CountingKillAdapter:
        def kill(self, pid: int) -> object:
            nonlocal kill_calls
            with kill_lock:
                kill_calls += 1
            return reap_process_group(pid, grace_seconds=0.25)

    stubborn = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; sleep 30"], start_new_session=True)
    time.sleep(0.05)
    stubborn_worker = _ActiveWorker(adapter=_CountingKillAdapter(), proc=stubborn, pid=stubborn.pid)
    try:
        threads = [threading.Thread(target=_reap_worker, args=(stubborn_worker,)) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=3.0)
        assert kill_calls == 1
        assert not _pid_alive(stubborn.pid)
    finally:
        if _pid_alive(stubborn.pid):
            reap_process_group(stubborn.pid, grace_seconds=0.1)


def test_session_load_rejects_relative_cwd_in_persisted_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted metadata with relative 'cwd' (e.g. '.') must be rejected even if process CWD matches."""
    monkeypatch.chdir(tmp_path)
    acp_dir = tmp_path / ".sdd" / "runtime" / "acp_sessions"
    acp_dir.mkdir(parents=True, exist_ok=True)
    (acp_dir / "sess-rel.json").write_text(
        json.dumps({"session_id": "sess-rel", "cwd": ".", "run_id": "sess-rel", "mode": "manual"}),
        encoding="utf-8",
    )

    assert load_persisted_session(str(tmp_path), "sess-rel") == (None, None)

    server = build_default_server(server_url=None, adapter_name="mock")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as exc_info:
            await server.registry.dispatch(
                ACPRequestContext("session/load", 2),
                {"sessionId": "sess-rel", "cwd": str(tmp_path), "mcpServers": []},
            )
        assert exc_info.value.code == RESOURCE_NOT_FOUND

    asyncio.run(_run())


def test_session_load_nul_cwd_returns_invalid_params_and_safe_id_rejects_trailing_newline(
    tmp_path: Path,
) -> None:
    """NUL byte in session/load cwd must return -32602 (INVALID_PARAMS), and _SAFE_ID_RE uses \\Z."""
    assert _SAFE_ID_RE.match("sess-valid\n") is None

    with pytest.raises(ACPSchemaError) as schema_exc:
        validate_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/load",
                "params": {"sessionId": "sess-1", "cwd": f"{tmp_path}\x00tail", "mcpServers": []},
            }
        )
    assert schema_exc.value.code == INVALID_PARAMS

    server = build_default_server(server_url=None, adapter_name="mock")

    async def _run() -> None:
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        with pytest.raises(ACPSchemaError) as dispatch_exc:
            await server.registry.dispatch(
                ACPRequestContext("session/load", 2),
                {"sessionId": "sess-1", "cwd": f"{tmp_path}\x00tail", "mcpServers": []},
            )
        assert dispatch_exc.value.code == INVALID_PARAMS

    asyncio.run(_run())


def test_session_load_rejects_intra_runs_symlinked_journal(tmp_path: Path) -> None:
    """session/load must reject journal paths escaping .sdd/runs/<run_id> into sibling runs."""
    foreign = EventJournal("cli-run-sibling", tmp_path / ".sdd")
    foreign.record("agent_message", text="SIBLING_RUN_SECRET_PAYLOAD")

    frames: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    server1 = build_default_server(server_url=None, adapter_name="mock", stream_publisher=_publish)
    sid_file_link = ""
    sid_dir_link = ""

    async def _setup_and_check_inmem() -> None:
        nonlocal sid_file_link, sid_dir_link
        await server1.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        c1 = await server1.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        c2 = await server1.registry.dispatch(
            ACPRequestContext("session/new", 3),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        sid_file_link = c1["sessionId"]
        sid_dir_link = c2["sessionId"]

        run_dir_1 = tmp_path / ".sdd" / "runs" / sid_file_link
        run_dir_1.mkdir(parents=True, exist_ok=True)
        (run_dir_1 / "journal.jsonl").symlink_to("../cli-run-sibling/journal.jsonl")

        (tmp_path / ".sdd" / "runs" / sid_dir_link).symlink_to("cli-run-sibling")

        for bad_sid in (sid_file_link, sid_dir_link):
            with pytest.raises(ACPSchemaError) as inmem_exc:
                await server1.registry.dispatch(
                    ACPRequestContext("session/load", 4),
                    {"sessionId": bad_sid, "cwd": str(tmp_path), "mcpServers": []},
                )
            assert inmem_exc.value.code == RESOURCE_NOT_FOUND

    asyncio.run(_setup_and_check_inmem())

    server2 = build_default_server(server_url=None, adapter_name="mock", stream_publisher=_publish)

    async def _check_persisted() -> None:
        await server2.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        for bad_sid in (sid_file_link, sid_dir_link):
            with pytest.raises(ACPSchemaError) as disk_exc:
                await server2.registry.dispatch(
                    ACPRequestContext("session/load", 2),
                    {"sessionId": bad_sid, "cwd": str(tmp_path), "mcpServers": []},
                )
            assert disk_exc.value.code == RESOURCE_NOT_FOUND

    asyncio.run(_check_persisted())
    assert "SIBLING_RUN_SECRET_PAYLOAD" not in json.dumps(frames)


def test_http_v1_manual_mode_without_client_requester_fails_fast_without_legacy_permission_frame(
    tmp_path: Path,
) -> None:
    """HTTP ACP v1 manual-mode turns with client_requester=None must fail closed immediately without legacy frames."""
    (tmp_path / "bernstein.yaml").write_text(
        "approvals:\n  interactive: true\n  timeout_seconds: 30\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    server = build_default_server(server_url=None, adapter_name="mock")
    server.registry.permission_timeout_s = 8.0
    http = server.http_transport()

    sse_frames: list[dict[str, Any]] = []
    prompt_response: dict[str, Any] = {}
    sid = ""

    async def _post_json(payload: dict[str, Any]) -> dict[str, Any]:
        status, _headers, body = await http.handle_request(
            json.dumps(payload).encode("utf-8"),
            "application/json",
            peer="http://127.0.0.1",
        )
        assert status == 200
        assert isinstance(body, (bytes, bytearray))
        return json.loads(body.decode("utf-8"))

    async def _run() -> None:
        nonlocal prompt_response, sid
        init_env = await _post_json(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
        )
        assert init_env["result"]["protocolVersion"] == 1

        new_env = await _post_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}}
        )
        sid = new_env["result"]["sessionId"]
        assert new_env["result"]["modes"]["currentModeId"] == "manual"
        assert server.registry.client_requester is None

        direct_t0 = time.monotonic()
        direct_decision, direct_kind = await asyncio.wait_for(
            server.registry.ask_permission_outcome(sid, "edit_file", "edit app.py"),
            timeout=1.5,
        )
        assert time.monotonic() - direct_t0 < 0.5
        assert direct_decision == "rejected"
        assert direct_kind == "transport_unsupported"

        t0 = time.monotonic()
        status, _headers, stream = await http.handle_request(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "Edit app.py"}]},
                }
            ).encode("utf-8"),
            "text/event-stream",
            peer="http://127.0.0.1",
        )
        assert status == 200
        assert not isinstance(stream, (bytes, bytearray))

        async def _collect_sse() -> None:
            nonlocal prompt_response
            async for chunk in stream:
                for line in chunk.decode("utf-8").splitlines():
                    if line.startswith("data: "):
                        frame = json.loads(line[len("data: ") :])
                        sse_frames.append(frame)
                        if "id" in frame and frame.get("id") == 3:
                            prompt_response = frame

        await asyncio.wait_for(_collect_sse(), timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, f"HTTP v1 manual turn hung waiting on permission timeout: {elapsed:.3f}s"

    asyncio.run(_run())

    assert not any(f.get("method") == "requestPermission" for f in sse_frames)
    assert prompt_response.get("result", {}).get("stopReason") == "end_turn"

    updates = [f["params"]["update"] for f in sse_frames if f.get("method") == "session/update"]
    assert any(u.get("sessionUpdate") == "tool_call_update" and u.get("status") == "failed" for u in updates)
    agent_msgs = [
        u.get("content", {}).get("text", "") for u in updates if u.get("sessionUpdate") == "agent_message_chunk"
    ]
    assert any("session/request_permission" in m and "auto" in m for m in agent_msgs)

    store = TaskStore(tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    store.replay_jsonl()
    settled = store.list_tasks()[0]
    assert settled.status == TaskStatus.FAILED

    journal_events = load_events(tmp_path / ".sdd" / "runs" / sid / "journal.jsonl").events
    failed_reasons = [str(e.get("reason", "")) for e in journal_events if e.get("event") == "task_failed"]
    assert failed_reasons and "session/request_permission" in failed_reasons[-1] and "auto" in failed_reasons[-1]


def test_session_load_post_await_latest_recheck_rejects_relative_and_nul_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-await latest TOCTOU recheck in session/load must reject relative and NUL-containing cwd with -32002."""
    monkeypatch.chdir(tmp_path)
    server = build_default_server(server_url=None, adapter_name="mock")
    injected_cwd = "."

    async def _racing_loader(req_cwd_str: str, sid: str) -> tuple[ACPSession | None, list[dict[str, Any]]]:
        await server.registry.sessions.upsert(ACPSession(session_id=sid, cwd=injected_cwd, protocol_version=1))
        return ACPSession(session_id=sid, cwd=req_cwd_str, protocol_version=1), []

    server.registry.session_loader = _racing_loader

    async def _run() -> None:
        nonlocal injected_cwd
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})

        injected_cwd = "."
        rel_env = await dispatch_frame(
            server.registry,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/load",
                "params": {"sessionId": "sess-race-rel", "cwd": str(tmp_path), "mcpServers": []},
            },
            peer="stdio",
        )
        assert rel_env is not None and rel_env.get("error", {}).get("code") == RESOURCE_NOT_FOUND

        injected_cwd = f"{tmp_path}\x00evil"
        nul_env = await dispatch_frame(
            server.registry,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/load",
                "params": {"sessionId": "sess-race-nul", "cwd": str(tmp_path), "mcpServers": []},
            },
            peer="stdio",
        )
        assert nul_env is not None and nul_env.get("error", {}).get("code") == RESOURCE_NOT_FOUND

    asyncio.run(_run())
