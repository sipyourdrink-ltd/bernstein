"""Unit tests for PR 2 ACP v1 features: EventJournal projection, ``session/load``,
``allow_always`` permission promotion, ``run_terminal_setup``, and ``auth_headers()``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bernstein.cli.commands.acp_cmd import run_terminal_setup
from bernstein.core.approval.queue import ApprovalQueue
from bernstein.core.protocols.acp.handlers import ACPHandlerRegistry, ACPRequestContext
from bernstein.core.protocols.acp.projection import (
    JournalACPProjector,
    infer_tool_kind,
    project_journal_to_acp_updates,
)
from bernstein.core.protocols.acp.runner import EmbeddedOrchestratorRunner
from bernstein.core.protocols.acp.schema import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    RESOURCE_NOT_FOUND,
    ACPSchemaError,
    validate_request,
)
from bernstein.core.protocols.acp.server import build_default_server
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.replay.journal import EventJournal
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore


def test_schema_validates_session_load() -> None:
    parsed = validate_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/load",
            "params": {"sessionId": "sess-1", "cwd": "/tmp/ws", "mcpServers": []},
        }
    )
    assert parsed.method == "session/load"
    assert parsed.params["sessionId"] == "sess-1"

    with pytest.raises(ACPSchemaError) as missing_sid:
        validate_request(
            {"jsonrpc": "2.0", "id": 7, "method": "session/load", "params": {"cwd": "/tmp/ws", "mcpServers": []}}
        )
    assert missing_sid.value.code == INVALID_PARAMS

    with pytest.raises(ACPSchemaError) as rel_cwd:
        validate_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/load",
                "params": {"sessionId": "sess-1", "cwd": "relative/ws", "mcpServers": []},
            }
        )
    assert rel_cwd.value.code == INVALID_PARAMS


def test_journal_projection_covers_plan_tools_chunks_diffs_and_usage(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-proj-1", sdd)
    journal.record("user_prompt", text="Refactor helper")
    journal.record(
        "plan.graph.full",
        goal="Refactor helper",
        tasks=[{"id": "t1", "title": "Refactor helper.py", "priority": 1}],
    )
    journal.record("agent_thought", text="Inspecting helper.py")
    journal.record(
        "task_claimed", task_id="t1", title="Refactor helper.py", tool="edit_file", args={"path": "helper.py"}
    )
    journal.record("task_diff_captured", task_id="t1", path="helper.py", old_text="x=1\n", new_text="x = 1\n")
    journal.record("task_completed", task_id="t1", summary="Updated formatting")
    journal.record("usage_update", input_tokens=120, output_tokens=45, thought_tokens=15, size=128000, cost_usd=0.002)
    journal.record("agent_message", text="Helper refactored cleanly.")

    updates = project_journal_to_acp_updates(journal.path, include_user_messages=True)
    kinds = [u["sessionUpdate"] for u in updates]
    assert "user_message_chunk" in kinds
    assert "plan" in kinds
    assert "agent_thought_chunk" in kinds
    assert "tool_call" in kinds
    assert "tool_call_update" in kinds
    assert "usage_update" in kinds
    assert "agent_message_chunk" in kinds

    # Final plan update marks t1 completed with high priority.
    plans = [u for u in updates if u["sessionUpdate"] == "plan"]
    assert plans[-1]["entries"] == [{"content": "Refactor helper.py", "priority": "high", "status": "completed"}]

    # Diff update carries ACP v1 FileEditToolCallContent structure.
    diff_updates = [u for u in updates if u["sessionUpdate"] == "tool_call_update" and "content" in u]
    assert any(u["content"][0]["type"] == "diff" and u["content"][0]["path"] == "helper.py" for u in diff_updates)

    # Usage update carries used, size, and USD cost.
    usage_upd = next(u for u in updates if u["sessionUpdate"] == "usage_update")
    assert usage_upd["used"] == 180
    assert usage_upd["size"] == 128000
    assert usage_upd["cost"] == {"amount": 0.002, "currency": "USD"}


def test_infer_tool_kind_mappings() -> None:
    assert infer_tool_kind("read_file") == "read"
    assert infer_tool_kind("edit_file") == "edit"
    assert infer_tool_kind("delete_path") == "delete"
    assert infer_tool_kind("rename_symbol") == "move"
    assert infer_tool_kind("grep_code") == "search"
    assert infer_tool_kind("run_pytest") == "execute"
    assert infer_tool_kind("http_fetch") == "fetch"
    assert infer_tool_kind("plan_step") == "think"
    assert infer_tool_kind("custom_widget") == "other"


def test_session_load_pre_init_and_unknown_and_allow_always_promotion(tmp_path: Path) -> None:
    frames: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    registry = ACPHandlerRegistry(sessions=ACPSessionStore(), stream_publisher=_publish)

    async def _run() -> None:
        # Pre-init session/load rejected with -32600.
        with pytest.raises(ACPSchemaError) as pre_exc:
            await registry.dispatch(
                ACPRequestContext("session/load", 1),
                {"sessionId": "s1", "cwd": str(tmp_path), "mcpServers": []},
            )
        assert pre_exc.value.code == INVALID_REQUEST

        init = await registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        assert init["agentCapabilities"]["loadSession"] is True

        # Unknown session rejected with -32002.
        with pytest.raises(ACPSchemaError) as missing_exc:
            await registry.dispatch(
                ACPRequestContext("session/load", 2),
                {"sessionId": "sess-ghost", "cwd": str(tmp_path), "mcpServers": []},
            )
        assert missing_exc.value.code == RESOURCE_NOT_FOUND

        # allow_always caches permission for the tool on the session without re-prompting.
        created = await registry.dispatch(
            ACPRequestContext("session/new", 3),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        sid = created["sessionId"]
        rpc_calls = 0

        async def _always_allow(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            nonlocal rpc_calls
            rpc_calls += 1
            return {"outcome": {"outcome": "selected", "optionId": "allow_always"}}

        registry.client_requester = _always_allow
        assert await registry.ask_permission(sid, "edit_file", "edit a.py") == "approved"
        assert await registry.ask_permission(sid, "edit_file", "edit b.py") == "approved"
        assert rpc_calls == 1

    asyncio.run(_run())


def test_embedded_runner_custom_worker_and_cancel(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text("approvals:\n  interactive: false\n", encoding="utf-8")
    server = build_default_server(server_url=None, adapter_name="mock")
    assert server.registry.prompt_runner is not None

    async def _run() -> None:
        started = asyncio.Event()

        async def _slow_worker(_sess: Any, _prompt: str, _wd: Path, cancel_ev: asyncio.Event) -> str:
            started.set()
            await cancel_ev.wait()
            return "cancelled"

        runner = EmbeddedOrchestratorRunner(registry=server.registry, worker_executor=_slow_worker)
        server.registry.prompt_runner = runner.run_prompt

        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        sess = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        sid = sess["sessionId"]
        await server.registry.dispatch(
            ACPRequestContext("session/set_mode", 3),
            {"sessionId": sid, "modeId": "auto"},
        )
        turn = asyncio.create_task(
            server.registry.dispatch(
                ACPRequestContext("session/prompt", 4),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "Wait for cancel"}]},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await server.registry.dispatch(ACPRequestContext("session/cancel", None), {"sessionId": sid})
        res = await asyncio.wait_for(turn, timeout=2.0)
        assert res["stopReason"] == "cancelled"

    asyncio.run(_run())


def test_run_terminal_setup_variants(tmp_path: Path) -> None:
    assert run_terminal_setup(adapter_name="mock", env={}) == 0
    assert run_terminal_setup(adapter_name=None, env={"PATH": str(tmp_path)}) == 1
    fake_bin = tmp_path / "claude"
    fake_bin.write_text("#!/bin/sh\necho 1.0.0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    assert run_terminal_setup(adapter_name="claude", env={"PATH": str(tmp_path)}) == 0
    assert run_terminal_setup(adapter_name="codex", env={"PATH": str(tmp_path)}) == 1


def test_projector_direct_tool_call_and_completion() -> None:
    proj = JournalACPProjector()
    u1 = proj.feed_event("tool_call", {"tool_call_id": "tc-1", "tool": "read_file", "path": "README.md"})
    assert u1[0]["sessionUpdate"] == "tool_call"
    assert u1[0]["kind"] == "read"
    assert u1[0]["locations"] == [{"path": "README.md"}]
    u2 = proj.feed_event("tool_call_completed", {"tool_call_id": "tc-1", "output": "contents"})
    assert u2[0]["sessionUpdate"] == "tool_call_update"
    assert u2[0]["status"] == "completed"


def test_embedded_runner_mid_permission_cancel_cleans_queue_and_task(tmp_path: Path) -> None:
    """Cancelling while ``session/request_permission`` is pending resolves immediately and cleans state."""
    (tmp_path / "bernstein.yaml").write_text(
        "approvals:\n  interactive: true\n  timeout_seconds: 30\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    server = build_default_server(server_url=None, adapter_name="mock")

    async def _run() -> None:
        perm_asked = asyncio.Event()
        never_answer = asyncio.Event()

        async def _blocking_requester(method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
            if method == "session/request_permission":
                perm_asked.set()
                await never_answer.wait()
            return {}

        server.registry.client_requester = _blocking_requester
        await server.registry.dispatch(ACPRequestContext("initialize", 1), {"protocolVersion": 1})
        sess = await server.registry.dispatch(
            ACPRequestContext("session/new", 2),
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        sid = sess["sessionId"]

        turn = asyncio.create_task(
            server.registry.dispatch(
                ACPRequestContext("session/prompt", 3),
                {"sessionId": sid, "prompt": [{"type": "text", "text": "Edit app.py"}]},
            )
        )
        await asyncio.wait_for(perm_asked.wait(), timeout=3.0)
        await server.registry.dispatch(ACPRequestContext("session/cancel", None), {"sessionId": sid})
        res = await asyncio.wait_for(turn, timeout=2.0)
        assert res["stopReason"] == "cancelled"

        store = TaskStore(tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
        store.replay_jsonl()
        assert len(store._tasks) == 1
        assert next(iter(store._tasks.values())).status == TaskStatus.CANCELLED

        queue = ApprovalQueue(tmp_path / ".sdd" / "runtime" / "approvals", workdir=tmp_path)
        assert queue.list_pending(session_id=sid) == []

    asyncio.run(_run())
