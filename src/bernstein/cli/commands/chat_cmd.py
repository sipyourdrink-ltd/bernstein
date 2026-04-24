"""``bernstein chat`` CLI group -- connect chat clients to the orchestra.

Three subcommands:

  * ``chat serve``  -- run a bridge until interrupted.
  * ``chat status`` -- list active bindings from ``.sdd/chat/``.
  * ``chat logout`` -- clear local credentials and bindings for a driver.

The heavy lifting lives in :mod:`bernstein.core.chat`. This module is a
thin translator between click flags / env vars and the driver
primitives, plus the inbound slash-command glue that dispatches into
the task-server and security layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from bernstein.core.chat import (
    AllowList,
    Binding,
    BindingStore,
    BridgeProtocol,
    load_allow_list,
    load_driver,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.chat.bridge import ChatMessage

__all__ = ["chat_group"]

logger = logging.getLogger(__name__)

_DEFAULT_WORKDIR = Path.cwd
_PLATFORM_CHOICES = ("telegram", "discord", "slack")
_ENV_TOKEN_MAP = {
    "telegram": "BERNSTEIN_TELEGRAM_TOKEN",
    "discord": "BERNSTEIN_DISCORD_TOKEN",
    "slack": "BERNSTEIN_SLACK_TOKEN",
}


@click.group("chat")
def chat_group() -> None:
    """Drive Bernstein agents from Telegram, Discord, or Slack."""


@chat_group.command("serve")
@click.option(
    "--platform",
    type=click.Choice(_PLATFORM_CHOICES),
    required=True,
    help="Chat platform to connect to.",
)
@click.option(
    "--token",
    default=None,
    help="Bot token. Falls back to $BERNSTEIN_<PLATFORM>_TOKEN when omitted.",
)
@click.option(
    "--allow",
    default=None,
    help="Comma-separated user ids allowed to drive agents. Merges with bernstein.yaml.",
)
def chat_serve(platform: str, token: str | None, allow: str | None) -> None:
    """Run the chat bridge for PLATFORM until Ctrl-C."""
    workdir = _DEFAULT_WORKDIR()
    resolved_token = token or os.environ.get(_ENV_TOKEN_MAP[platform], "")
    if platform == "telegram" and not resolved_token:
        raise click.UsageError(
            "Telegram requires --token or $BERNSTEIN_TELEGRAM_TOKEN.",
        )

    overrides = _split_allow(allow)
    allow_list = load_allow_list(workdir / "bernstein.yaml", cli_override=overrides)
    bindings = BindingStore(workdir)
    driver_cls: Any = load_driver(platform)
    bridge: BridgeProtocol = driver_cls(resolved_token)
    session = ChatSession(bridge, bindings, allow_list, workdir)
    session.install_handlers()

    click.echo(f"chat: starting {platform} bridge (workdir={workdir})")
    try:
        asyncio.run(session.run_forever())
    except KeyboardInterrupt:
        click.echo("chat: interrupted, shutting down.")
    except NotImplementedError as exc:
        click.echo(f"chat: {exc}", err=True)
        raise SystemExit(2) from exc


@chat_group.command("status")
def chat_status() -> None:
    """Print active chat<->session bindings."""
    workdir = _DEFAULT_WORKDIR()
    store = BindingStore(workdir)
    entries = store.all()
    if not entries:
        click.echo("chat: no active bindings.")
        return
    for binding in entries:
        click.echo(
            f"{binding.platform:<9} thread={binding.thread_id:<16} "
            f"session={binding.session_id or '-':<14} "
            f"task={binding.task_id or '-':<10} "
            f"adapter={binding.adapter or '-'}",
        )


@chat_group.command("logout")
@click.option(
    "--platform",
    type=click.Choice(_PLATFORM_CHOICES),
    required=True,
    help="Platform whose local state should be cleared.",
)
def chat_logout(platform: str) -> None:
    """Drop cached bindings for PLATFORM."""
    workdir = _DEFAULT_WORKDIR()
    store = BindingStore(workdir)
    removed = 0
    for binding in store.all():
        if binding.platform == platform and store.delete(binding.platform, binding.thread_id):
            removed += 1
    click.echo(f"chat: removed {removed} binding(s) for {platform}.")


# ---------------------------------------------------------------------------
# Session orchestration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TaskDispatcher:
    """Strategy interface for creating tasks from chat goals.

    Kept behind a tiny abstraction so tests can inject a fake without
    spinning up the full task-server.
    """

    workdir: Path

    async def create(self, *, goal: str, adapter: str, thread_id: str) -> tuple[str, str]:
        """Create a task and return ``(task_id, session_id)``.

        The default implementation defers to
        :func:`_create_task_via_store` which wires into
        ``TaskStore.create`` using the task-server's JSONL backing file.
        Tests monkeypatch this method directly.
        """
        # ``adapter`` is kept on the caller's side for logging / status
        # display; the underlying TaskStore only needs goal + thread id.
        del adapter
        return await _create_task_via_store(
            goal=goal,
            thread_id=thread_id,
            workdir=self.workdir,
        )


class ChatSession:
    """Glue between a :class:`BridgeProtocol` and the task-server.

    One instance is created per ``chat serve`` invocation. It owns the
    :class:`BindingStore`, the allow-list gate, and the six slash
    commands the bot understands.
    """

    def __init__(
        self,
        bridge: BridgeProtocol,
        bindings: BindingStore,
        allow_list: AllowList,
        workdir: Path,
        dispatcher: _TaskDispatcher | None = None,
    ) -> None:
        self.bridge = bridge
        self.bindings = bindings
        self.allow_list = allow_list
        self.workdir = workdir
        self.dispatcher = dispatcher or _TaskDispatcher(workdir=workdir)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def install_handlers(self) -> None:
        """Register every slash command and the approval-button hook."""
        self.bridge.on_command("run", self._on_run)
        self.bridge.on_command("status", self._on_status)
        self.bridge.on_command("approve", self._on_approve)
        self.bridge.on_command("reject", self._on_reject)
        self.bridge.on_command("switch", self._on_switch)
        self.bridge.on_command("stop", self._on_stop)
        self.bridge.on_button(self._on_button)

    async def run_forever(self) -> None:
        """Start the bridge and sleep until cancelled."""
        await self.bridge.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.bridge.stop()

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _on_run(self, msg: ChatMessage) -> None:
        if not await self._gate(msg):
            return
        goal = _extract_quoted_goal(msg)
        if not goal:
            await self.bridge.send_message(
                msg.thread_id,
                'Usage: /run "<goal>"',
            )
            return
        adapter = _default_adapter()
        try:
            task_id, session_id = await self.dispatcher.create(
                goal=goal,
                adapter=adapter,
                thread_id=msg.thread_id,
            )
        except Exception as exc:
            logger.exception("chat: failed to create task")
            await self.bridge.send_message(msg.thread_id, f"Could not create task: {exc}")
            return

        status_text = f"Task {task_id} queued -- adapter={adapter}"
        status_msg = await self.bridge.send_message(msg.thread_id, status_text)
        self.bindings.put(
            Binding(
                platform=self.bridge.platform,
                thread_id=msg.thread_id,
                session_id=session_id,
                task_id=task_id,
                adapter=adapter,
                goal=goal,
                status_message_id=status_msg,
            ),
        )

    async def _on_status(self, msg: ChatMessage) -> None:
        if not await self._gate(msg):
            return
        binding = self.bindings.get(self.bridge.platform, msg.thread_id)
        if binding is None:
            await self.bridge.send_message(msg.thread_id, "No active session in this thread.")
            return
        await self.bridge.send_message(
            msg.thread_id,
            (
                f"Session {binding.session_id or '-'} / task {binding.task_id or '-'}\n"
                f"Adapter: {binding.adapter or '-'}\n"
                f"Goal: {binding.goal or '-'}"
            ),
        )

    async def _on_approve(self, msg: ChatMessage) -> None:
        if not await self._gate(msg):
            return
        await self._resolve_oldest_pending(msg.thread_id, decision="approve")

    async def _on_reject(self, msg: ChatMessage) -> None:
        if not await self._gate(msg):
            return
        await self._resolve_oldest_pending(msg.thread_id, decision="reject")

    async def _on_switch(self, msg: ChatMessage) -> None:
        if not await self._gate(msg):
            return
        if not msg.args:
            await self.bridge.send_message(msg.thread_id, "Usage: /switch <adapter>")
            return
        new_adapter = msg.args[0]
        binding = self.bindings.get(self.bridge.platform, msg.thread_id)
        if binding is None or not binding.goal:
            await self.bridge.send_message(
                msg.thread_id,
                "No active session to switch; start one with /run first.",
            )
            return
        try:
            task_id, session_id = await self.dispatcher.create(
                goal=binding.goal,
                adapter=new_adapter,
                thread_id=msg.thread_id,
            )
        except Exception as exc:
            logger.exception("chat: failed to re-dispatch")
            await self.bridge.send_message(msg.thread_id, f"Switch failed: {exc}")
            return
        binding.adapter = new_adapter
        binding.task_id = task_id
        binding.session_id = session_id
        self.bindings.put(binding)
        await self.bridge.send_message(
            msg.thread_id,
            f"Re-dispatched to {new_adapter} (task {task_id}).",
        )

    async def _on_stop(self, msg: ChatMessage) -> None:
        if not await self._gate(msg):
            return
        binding = self.bindings.get(self.bridge.platform, msg.thread_id)
        if binding is None:
            await self.bridge.send_message(msg.thread_id, "No active session to stop.")
            return
        stop_marker = self.workdir / ".sdd" / "runtime" / "chat_stop"
        stop_marker.mkdir(parents=True, exist_ok=True)
        (stop_marker / f"{binding.session_id or binding.task_id or 'unknown'}.stop").write_text(
            "requested\n",
            encoding="utf-8",
        )
        self.bindings.delete(self.bridge.platform, msg.thread_id)
        await self.bridge.send_message(msg.thread_id, "Stop requested. Session will wind down gracefully.")

    async def _on_button(self, thread_id: str, approval_id: str, decision: str) -> None:
        """Persist an approve/reject decision to the security handshake dir."""
        _write_approval_decision(self.workdir, approval_id, decision)
        await self.bridge.send_message(
            thread_id,
            f"Approval {approval_id}: {decision}.",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _gate(self, msg: ChatMessage) -> bool:
        """Reject the message if the sender is not allow-listed."""
        if self.allow_list.is_allowed(msg.user_id):
            return True
        await self.bridge.send_message(
            msg.thread_id,
            "You are not authorized to drive agents from this chat.",
        )
        return False

    async def _resolve_oldest_pending(self, thread_id: str, *, decision: str) -> None:
        """Find the oldest pending approval and emit a decision file."""
        pending_dir = self.workdir / ".sdd" / "runtime" / "pending_approvals"
        if not pending_dir.exists():
            await self.bridge.send_message(thread_id, "No pending approvals.")
            return
        pending: list[Path] = sorted(
            (p for p in pending_dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        if not pending:
            await self.bridge.send_message(thread_id, "No pending approvals.")
            return
        approval_id = pending[0].stem
        _write_approval_decision(self.workdir, approval_id, decision)
        await self.bridge.send_message(
            thread_id,
            f"Approval {approval_id}: {decision}.",
        )

    async def stream_progress(self, binding: Binding, body: str) -> None:
        """Convenience: edit the thread's status message with ``body``.

        Provided so callers that poll the task-server can funnel
        progress through the bridge's debounced edit path.
        """
        if not binding.status_message_id:
            sent = await self.bridge.send_message(binding.thread_id, body)
            binding.status_message_id = sent
            self.bindings.put(binding)
            return
        await self.bridge.edit_message(binding.thread_id, binding.status_message_id, body)


# ---------------------------------------------------------------------------
# Task-server glue (kept module-private; swapped out in tests)
# ---------------------------------------------------------------------------


async def _create_task_via_store(
    *,
    goal: str,
    thread_id: str,
    workdir: Path,
) -> tuple[str, str]:
    """Create a task via :class:`TaskStore` and return ids.

    The task-server persists its state in JSONL at
    ``<workdir>/.sdd/runtime/tasks.jsonl`` (the stock layout). We open
    a new store against that file, call ``create(req)``, and return the
    task id plus a chat-scoped session id.
    """
    from bernstein.core.tasks.task_store import TaskStore

    tasks_path = workdir / ".sdd" / "runtime" / "tasks.jsonl"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    store = TaskStore(tasks_path)
    req = _ChatTaskRequest(
        title=goal[:80],
        description=goal,
        role="manager",
        priority=1,
        scope="chat",
        complexity="medium",
        task_type="feature",
    )
    task = await store.create(req)  # type: ignore[arg-type]
    session_id = f"chat-{thread_id}-{task.id}"
    return task.id, session_id


@dataclass(slots=True)
class _ChatTaskRequest:
    """Minimal ``TaskCreateRequest``-shaped payload for chat-sourced tasks.

    The full protocol has ~20 fields; we populate the ones ``create``
    actually reads and leave the rest at safe defaults. Kept local to
    this module because no other caller needs it.
    """

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    task_type: str
    estimated_minutes: int | None = None
    parent_task_id: str | None = None
    depends_on_repo: str | None = None
    tenant_id: str = "default"
    cell_id: str | None = None
    repo: str | None = None
    model: str | None = None
    effort: str | None = None
    batch_eligible: bool = False
    approval_required: bool = False
    eu_ai_act_risk: str = "minimal"
    risk_level: str = "low"
    parent_session_id: str | None = None
    parent_context: str | None = None
    retry_count: int | None = None
    max_retries: int | None = None
    retry_delay_s: float | None = None
    terminal_reason: str | None = None
    max_output_tokens: int | None = None

    @property
    def depends_on(self) -> tuple[str, ...]:
        return ()

    @property
    def owned_files(self) -> tuple[str, ...]:
        return ()

    @property
    def upgrade_details(self) -> None:
        return None

    @property
    def completion_signals(self) -> tuple[object, ...]:
        return ()

    @property
    def slack_context(self) -> None:
        return None

    @property
    def meta_messages(self) -> tuple[str, ...] | None:
        return None


def _write_approval_decision(workdir: Path, approval_id: str, decision: str) -> None:
    """Write ``<approval_id>.approved`` or ``.rejected`` for the security gate."""
    approvals_dir = workdir / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    suffix = "approved" if decision == "approve" else "rejected"
    (approvals_dir / f"{approval_id}.{suffix}").write_text(
        "via chat bridge\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _split_allow(value: str | None) -> Iterable[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _default_adapter() -> str:
    """Adapter to use when ``/run`` does not specify one."""
    return os.environ.get("BERNSTEIN_DEFAULT_CLI", "claude")


def _extract_quoted_goal(msg: ChatMessage) -> str:
    """Parse the goal from ``/run "..."`` style commands.

    ``shlex`` handles balanced quoting; if the user omitted quotes we
    still honour the remainder of the message as the goal.
    """
    # Drop the leading /run token.
    _, _, tail = msg.text.partition(" ")
    tail = tail.strip()
    if not tail:
        return ""
    try:
        tokens = shlex.split(tail)
    except ValueError:
        return tail
    if not tokens:
        return tail
    return tokens[0] if len(tokens) == 1 else " ".join(tokens)
