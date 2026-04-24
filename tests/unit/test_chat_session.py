"""Unit tests for the ChatSession glue that wires a bridge to the task-server."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bernstein.cli.commands.chat_cmd import (
    ChatSession,
    _extract_quoted_goal,
    _TaskDispatcher,
    _write_approval_decision,
)
from bernstein.core.chat import AllowList, Binding, BindingStore, PendingApproval
from bernstein.core.chat.bridge import BridgeProtocol, ChatMessage


@dataclass(slots=True)
class _FakeBridge(BridgeProtocol):
    platform: str = "telegram"
    sent: list[tuple[str, str]] = field(default_factory=list)
    edits: list[tuple[str, str, str]] = field(default_factory=list)
    cmd_handlers: dict[str, Any] = field(default_factory=dict)
    button_handler: Any = None
    approvals: list[PendingApproval] = field(default_factory=list)
    next_message_id: int = 1

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_message(self, thread_id: str, text: str) -> str:
        self.sent.append((thread_id, text))
        self.next_message_id += 1
        return str(self.next_message_id)

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        self.edits.append((thread_id, message_id, text))

    async def push_approval(self, approval: PendingApproval) -> str:
        self.approvals.append(approval)
        self.next_message_id += 1
        return str(self.next_message_id)

    def on_command(self, name: str, handler: Any) -> None:
        self.cmd_handlers[name] = handler

    def on_button(self, handler: Any) -> None:
        self.button_handler = handler


@dataclass(slots=True)
class _StubDispatcher:
    calls: list[dict[str, str]] = field(default_factory=list)

    async def create(self, *, goal: str, adapter: str, thread_id: str) -> tuple[str, str]: # NOSONAR — async-signature required by protocol
        self.calls.append({"goal": goal, "adapter": adapter, "thread_id": thread_id})
        tid = f"t-{len(self.calls)}"
        return tid, f"sess-{tid}"


def _make_session(tmp_path: Path, *, allow: set[str] | None = None) -> tuple[ChatSession, _FakeBridge, _StubDispatcher]:
    bridge = _FakeBridge()
    store = BindingStore(tmp_path)
    allow_list = AllowList(users=allow or {"7"})
    dispatcher = _StubDispatcher()
    session = ChatSession(
        bridge=bridge,
        bindings=store,
        allow_list=allow_list,
        workdir=tmp_path,
        dispatcher=_TaskDispatcher(workdir=tmp_path),  # type: ignore[arg-type]
    )
    # Replace with stub dispatcher so we never touch the real TaskStore.
    session.dispatcher = dispatcher  # type: ignore[assignment]
    session.install_handlers()
    return session, bridge, dispatcher


def test_run_creates_task_and_binding(tmp_path: Path) -> None:
    """``/run "goal"`` dispatches and records a binding."""
    session, bridge, dispatcher = _make_session(tmp_path)

    async def scenario() -> None:
        msg = ChatMessage(
            thread_id="42",
            user_id="7",
            text='/run "Add JWT auth"',
            args=['"Add', "JWT", 'auth"'],
        )
        await bridge.cmd_handlers["run"](msg)

    asyncio.run(scenario())

    assert dispatcher.calls == [
        {"goal": "Add JWT auth", "adapter": "claude", "thread_id": "42"},
    ]
    binding = session.bindings.get("telegram", "42")
    assert binding is not None
    assert binding.task_id == "t-1"
    assert binding.session_id == "sess-t-1"
    assert binding.goal == "Add JWT auth"


def test_run_rejects_unauthorised_user(tmp_path: Path) -> None:
    """A user outside the allow-list gets a refusal and no task is created."""
    session, bridge, dispatcher = _make_session(tmp_path, allow={"authorised"})

    async def scenario() -> None:
        await bridge.cmd_handlers["run"](
            ChatMessage(
                thread_id="42",
                user_id="intruder",
                text='/run "hack"',
            ),
        )

    asyncio.run(scenario())

    assert dispatcher.calls == []
    assert session.bindings.get("telegram", "42") is None
    assert any("not authorized" in text for _, text in bridge.sent)


def test_switch_redispatches_with_preserved_goal(tmp_path: Path) -> None:
    """``/switch`` re-runs the task on a new adapter with the original goal."""
    session, bridge, dispatcher = _make_session(tmp_path)

    # Seed an existing binding as if /run had been called.
    session.bindings.put(
        Binding(
            platform="telegram",
            thread_id="42",
            session_id="sess-old",
            task_id="t-old",
            adapter="claude",
            goal="Fix the bug",
        ),
    )

    async def scenario() -> None:
        await bridge.cmd_handlers["switch"](
            ChatMessage(
                thread_id="42",
                user_id="7",
                text="/switch codex",
                args=["codex"],
            ),
        )

    asyncio.run(scenario())

    assert dispatcher.calls == [
        {"goal": "Fix the bug", "adapter": "codex", "thread_id": "42"},
    ]
    updated = session.bindings.get("telegram", "42")
    assert updated is not None
    assert updated.adapter == "codex"
    assert updated.task_id == "t-1"


def test_approve_writes_decision_for_oldest_pending(tmp_path: Path) -> None:
    """``/approve`` drops a ``.approved`` file for the oldest pending request."""
    _session, bridge, _ = _make_session(tmp_path)

    pending_dir = tmp_path / ".sdd" / "runtime" / "pending_approvals"
    pending_dir.mkdir(parents=True)
    older = pending_dir / "t-1.json"
    newer = pending_dir / "t-2.json"
    older.write_text("{}")
    # Make the "newer" file have a later mtime.
    time.sleep(0.01)
    newer.write_text("{}")

    async def scenario() -> None:
        await bridge.cmd_handlers["approve"](
            ChatMessage(thread_id="42", user_id="7", text="/approve"),
        )

    asyncio.run(scenario())

    approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
    assert (approvals_dir / "t-1.approved").exists()
    assert not (approvals_dir / "t-2.approved").exists()


def test_stop_clears_binding_and_drops_marker(tmp_path: Path) -> None:
    """``/stop`` removes the binding and writes a stop marker file."""
    session, bridge, _ = _make_session(tmp_path)
    session.bindings.put(
        Binding(
            platform="telegram",
            thread_id="42",
            session_id="sess-x",
            task_id="t-1",
            adapter="claude",
            goal="ship it",
        ),
    )

    async def scenario() -> None:
        await bridge.cmd_handlers["stop"](
            ChatMessage(thread_id="42", user_id="7", text="/stop"),
        )

    asyncio.run(scenario())

    assert session.bindings.get("telegram", "42") is None
    marker = tmp_path / ".sdd" / "runtime" / "chat_stop" / "sess-x.stop"
    assert marker.exists()


def test_button_handler_writes_rejection_file(tmp_path: Path) -> None:
    """An inline reject-button press should persist ``<id>.rejected``."""
    _session, bridge, _ = _make_session(tmp_path)

    async def scenario() -> None:
        assert bridge.button_handler is not None
        await bridge.button_handler("42", "t-42", "reject")

    asyncio.run(scenario())

    approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
    assert (approvals_dir / "t-42.rejected").exists()


def test_write_approval_decision_creates_approved_file(tmp_path: Path) -> None:
    """Direct helper produces the security-layer handshake file."""
    _write_approval_decision(tmp_path, "t-99", "approve")
    assert (tmp_path / ".sdd" / "runtime" / "approvals" / "t-99.approved").exists()


def test_extract_quoted_goal_handles_plain_and_quoted() -> None:
    """``_extract_quoted_goal`` preserves unquoted goals and unwraps quotes."""
    single = _extract_quoted_goal(ChatMessage(thread_id="t", user_id="u", text='/run "one thing"'))
    assert single == "one thing"

    plain = _extract_quoted_goal(ChatMessage(thread_id="t", user_id="u", text="/run no quotes"))
    assert plain == "no quotes"

    empty = _extract_quoted_goal(ChatMessage(thread_id="t", user_id="u", text="/run"))
    assert empty == ""


@pytest.mark.parametrize("missing_dir", [True, False])
def test_approve_with_no_pending_reports_clean_state(tmp_path: Path, missing_dir: bool) -> None:
    """``/approve`` gracefully reports when there's nothing to resolve."""
    _session, bridge, _ = _make_session(tmp_path)
    if not missing_dir:
        (tmp_path / ".sdd" / "runtime" / "pending_approvals").mkdir(parents=True)

    async def scenario() -> None:
        await bridge.cmd_handlers["approve"](
            ChatMessage(thread_id="42", user_id="7", text="/approve"),
        )

    asyncio.run(scenario())

    assert any("No pending approvals" in text for _, text in bridge.sent)
