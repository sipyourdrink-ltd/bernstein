"""Discord driver -- bidirectional bot bridge with attested approvals.

Standard Discord bot integration: configure a bot token from the
Discord developer portal and the bridge connects via the gateway
WebSocket. ``pip install 'bernstein[discord]'`` pulls in the
``discord.py`` library.

The library import is guarded so the module can always be imported --
``discord.py`` is only required when :meth:`DiscordBridge.start` actually
runs. This keeps ``bernstein chat serve --platform=telegram`` working
for operators who only installed the Telegram extra.

Key behaviours:

  * **Slash commands.** Handlers registered via :meth:`on_command` are
    routed based on the application-command name in the Discord
    interaction event. Options arrive as ``name=value`` tokens in the
    :class:`~bernstein.core.chat.bridge.ChatMessage.args` list so
    handlers can read structured arguments without re-parsing.
  * **Approval buttons.** :meth:`push_approval` renders an action row
    with two buttons whose ``custom_id`` is ``approve:<id>`` /
    ``reject:<id>``. Decoding a component interaction is symmetric:
    read ``custom_id``, split on ``:``, dispatch to the registered
    handler. No extra state is needed.
  * **Edit throttle.** :meth:`edit_message` is debounced to one edit
    per ``(channel, message)`` per :data:`EDIT_THROTTLE_S` seconds.
    Discord's per-route rate limit on message edits kicks in around
    five requests per five seconds; debouncing to one second matches
    the Slack driver's default and stays comfortably under the limit.
  * **Attested approvals.** Every Discord button press that resolves a
    pending approval is appended to the HMAC-chained audit log as a
    ``chat.discord.approval`` event whose ``details`` cover
    ``(approver, interaction_id, decision, tool_call_hash, worktree_id,
    partition_id)``; the chain's ``prev_hmac`` provides the
    ``prev_chain_digest`` link required by the AC.
  * **Worktree pinning.** Approvals carry a ``worktree_id`` so an
    ``/approve`` for a worker bound to ``wt-a`` cannot resolve a pending
    approval registered against a different worktree. Cross-worktree
    attempts log a ``chat.discord.approval_rejected`` audit entry so
    the bypass is visible to the operator.
  * **Channel-scoped scheduling fence.** Approvals also carry a
    ``partition_id`` resolved via
    :func:`~bernstein.core.orchestration.scheduler_partitions.partition_id_for_channel`.
    A click in channel A cannot resolve an approval registered against
    channel B -- this is the second consumer of the shared partition
    helper (Slack is the first).
  * **Outbound message signing.** Every outbound chat message carries an
    Ed25519 detached signature over ``(install_id, session_id,
    content_hash)``. The bridge mints (or reuses) the install's Ed25519
    keypair via the same on-disk layout the Slack driver uses so a
    recipient with the install's public key can confirm the message was
    not injected by another bernstein install impersonating the
    workspace.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import importlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bernstein.core.chat.bridge import (
    BridgeProtocol,
    ButtonHandler,
    ChatMessage,
    CommandHandler,
    PendingApproval,
    approval_body,
)
from bernstein.core.orchestration.scheduler_partitions import (
    PartitionViolationError,
    partition_id_for_channel,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditLog

__all__ = [
    "APPROVAL_EVENT_TYPE",
    "APPROVAL_REJECTED_EVENT_TYPE",
    "EDIT_THROTTLE_S",
    "ChannelPartitionMismatchError",
    "CrossWorktreeApprovalError",
    "DiscordBridge",
    "DiscordDependencyError",
    "PendingApprovalRecord",
    "verify_chat_signature",
]

logger = logging.getLogger(__name__)

#: Minimum seconds between consecutive edits to the same ``(channel, message)``.
#: Discord's per-route limit on message edits is approximately 5 requests
#: per 5 seconds; 1 second debounce mirrors the Slack default and stays
#: comfortably under the limit even with multiple concurrent streams.
EDIT_THROTTLE_S: float = 1.0

#: Audit-chain event type for an approval that resolved cleanly.
APPROVAL_EVENT_TYPE: str = "chat.discord.approval"

#: Audit-chain event type for an approval that was rejected by the
#: worktree-pinning or channel-partition guard.
APPROVAL_REJECTED_EVENT_TYPE: str = "chat.discord.approval_rejected"

#: Discord application-command interaction type constant.
_INTERACTION_APPLICATION_COMMAND: int = 2
#: Discord message-component interaction type constant.
_INTERACTION_COMPONENT: int = 3


class DiscordDependencyError(RuntimeError):
    """Raised when ``discord.py`` is not installed."""


class CrossWorktreeApprovalError(RuntimeError):
    """Raised when an /approve resolves a pending approval on a different worktree.

    The bridge logs the rejection into the audit chain before raising so
    operators can audit attempted bypasses.
    """


class ChannelPartitionMismatchError(RuntimeError):
    """Raised when an /approve arrives in a different channel partition.

    Mirrors :class:`CrossWorktreeApprovalError`: the rejection is written
    into the audit chain before this exception is raised so the bypass
    is visible to operators even when the calling code swallows the
    exception.
    """


@dataclass(slots=True)
class _EditState:
    """Per-message debouncing bookkeeping.

    Attributes:
        last_edit_ts: Monotonic timestamp of the last successful flush.
        pending_text: Latest body awaiting flush. Empty string means no
            pending write.
        task: Scheduled flush coroutine, if any.
    """

    last_edit_ts: float = 0.0
    pending_text: str = ""
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass(slots=True, frozen=True)
class PendingApprovalRecord:
    """Server-side bookkeeping for a pending Discord approval.

    The driver stores one of these per approval-id so resolution-time
    handlers can enforce worktree pinning *and* the channel-partition
    fence, and emit the chained audit entry with the tool-call digest
    the approval covers.
    """

    approval_id: str
    tool_call_hash: str
    worktree_id: str
    thread_id: str
    partition_id: str


class DiscordBridge(BridgeProtocol):
    """Discord implementation of :class:`BridgeProtocol` over the gateway.

    Args:
        token: Bot token from the Discord developer portal. Required.
        install_id: Stable identifier for this Bernstein install.
            Embedded in the signed envelope on every outbound message.
        session_id: Stable identifier for the active chat session.
            Bound into the signed envelope alongside ``install_id``.
        worktree_id: Identifier of the worktree this driver instance is
            bound to. The approval-resolution path refuses to settle any
            pending approval whose ``worktree_id`` differs.
        audit_log: Optional :class:`AuditLog`. When set, every approval
            resolution lands as a chained ``chat.discord.approval`` entry
            and every rejected cross-worktree or cross-partition attempt
            as a ``chat.discord.approval_rejected`` entry.
        key_dir: Filesystem directory backing the install's Ed25519
            keypair. Defaults to ``<workdir>/.bernstein/keys/discord``.
    """

    platform: str = "discord"

    def __init__(
        self,
        token: str,
        *,
        install_id: str = "",
        session_id: str = "",
        worktree_id: str = "",
        audit_log: AuditLog | None = None,
        key_dir: Path | None = None,
    ) -> None:
        if not token:
            raise ValueError("Discord bot token must be non-empty.")

        self._token = token
        self._install_id = install_id
        self._session_id = session_id
        self._worktree_id = worktree_id
        self._audit_log = audit_log

        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handler: ButtonHandler | None = None

        self._client: Any = None
        self._discord_mod: Any = None
        self._ui_mod: Any = None
        self._runner: asyncio.Task[None] | None = None

        self._edit_state: dict[str, _EditState] = {}
        self._edit_lock = asyncio.Lock()

        # Pending approvals registered by the orchestrator. Keyed by
        # approval_id so the resolution path can look the record up
        # without scanning the dict.
        self._pending_approvals: dict[str, PendingApprovalRecord] = {}
        # Resolved approvals -- public surface for replay / scheduler-state
        # reconstruction during tests and operator audit.
        self._approved_tool_call_hashes: set[str] = set()
        # Partition each approved hash was resolved on -- replayed for
        # the channel-scoped scheduling fence proof.
        self._approved_partition_for: dict[str, str] = {}
        # Sent-message bookkeeping so ``edit_message`` can re-find the
        # message handle the SDK returned without keeping the entire
        # discord.py ``Message`` graph alive.
        self._sent_messages: dict[str, Any] = {}
        # Last signed envelope emitted, exposed for tests/operators to
        # verify the outbound signature without scraping Discord's CDN.
        self._last_signed_envelope: dict[str, str] | None = None

        # Lazy keypair so importing this module never touches the filesystem.
        self._key_dir = key_dir
        self._private_key_pem: bytes | None = None
        self._public_key_pem: bytes | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on_command(self, name: str, handler: CommandHandler) -> None:
        """Register ``handler`` for the slash command ``/<name>``."""
        self._command_handlers[name.lstrip("/")] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        """Register the single approve/reject callback."""
        self._button_handler = handler

    def register_pending_approval(
        self,
        *,
        approval_id: str,
        tool_call_hash: str,
        worktree_id: str,
        thread_id: str,
        partition_id: str | None = None,
    ) -> None:
        """Tell the bridge a tool call is waiting for a Discord approval.

        The resolution path uses ``worktree_id`` to enforce worktree
        pinning, ``partition_id`` to enforce the channel-scoped
        scheduling fence, and ``tool_call_hash`` to populate the audit
        entry chained when the approver clicks Approve.

        ``partition_id`` defaults to the canonical
        ``discord:<thread_id>`` label so callers that do not opt into
        custom partition aliases still get the fence.
        """
        resolved_partition = partition_id or partition_id_for_channel(
            self.platform,
            thread_id,
        )
        self._pending_approvals[approval_id] = PendingApprovalRecord(
            approval_id=approval_id,
            tool_call_hash=tool_call_hash,
            worktree_id=worktree_id,
            thread_id=thread_id,
            partition_id=resolved_partition,
        )

    def approved_tool_call_hashes(self) -> set[str]:
        """Return a snapshot of every tool-call hash that has been approved."""
        return self._approved_tool_call_hashes.copy()

    def partition_assignments(self) -> dict[str, str]:
        """Return ``tool_call_hash -> partition_id`` for every approved call."""
        return self._approved_partition_for.copy()

    def public_key_pem(self) -> bytes:
        """Return the install's Ed25519 public key (PEM, SubjectPublicKeyInfo)."""
        self._ensure_keypair()
        assert self._public_key_pem is not None
        return self._public_key_pem

    def last_signed_envelope(self) -> dict[str, str] | None:
        """Return the last signed envelope minted for an outbound message.

        Discord does not surface message metadata to clients the way
        Slack does, so the envelope is exposed via this accessor for
        verifiers, audit pipelines, and tests that confirm the install
        signed what it sent.
        """
        return None if self._last_signed_envelope is None else dict(self._last_signed_envelope)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Discord and begin dispatching events.

        Wires the ``on_interaction`` event hook so application commands
        and component interactions flow into :meth:`dispatch_interaction`.
        The gateway connection itself runs in a background task so
        :meth:`start` returns promptly; ``stop`` cancels the task.
        """
        self._discord_mod = _import_discord()
        self._ui_mod = _import_discord_ui()
        intents_cls: Any = self._discord_mod.Intents
        intents = intents_cls.default()
        # Component interactions and slash commands arrive over the gateway
        # without needing the privileged ``message_content`` intent.
        client_cls: Any = self._discord_mod.Client
        self._client = client_cls(intents=intents)

        # ``@client.event`` decorates a coroutine and registers it as the
        # gateway handler for the corresponding ``on_*`` event name.
        bridge = self

        async def on_interaction(interaction: Any) -> None:
            await bridge.dispatch_interaction(interaction)

        # Bind the handler exactly the way discord.py does -- via the
        # decorator. The fake test client honours the same surface.
        self._client.event(on_interaction)

        # Discord's gateway connection runs forever; schedule it on a
        # background task so the caller's event loop can move on. The
        # task is cancelled in ``stop`` to release the connection.
        self._runner = asyncio.create_task(self._client.start(self._token))

    async def stop(self) -> None:
        """Flush pending edits and disconnect cleanly."""
        async with self._edit_lock:
            for state in self._edit_state.values():
                task = state.task
                if task is not None and not task.done():
                    task.cancel()
            self._edit_state.clear()

        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
            self._runner = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None
        self._discord_mod = None
        self._ui_mod = None

    # ------------------------------------------------------------------
    # Outbound primitives
    # ------------------------------------------------------------------

    async def send_message(self, thread_id: str, text: str) -> str:
        """Post ``text`` to ``thread_id`` and return the new message id.

        Outbound messages carry a signed envelope (recorded via
        :meth:`last_signed_envelope`) so a recipient with the install's
        public key can confirm the message originated from this workspace.
        """
        channel = await self._resolve_channel(thread_id)
        envelope = self._build_signed_envelope(text)
        sent = await channel.send(text)
        self._last_signed_envelope = envelope
        message_id = str(getattr(sent, "id", "") or "")
        if message_id:
            self._sent_messages[f"{thread_id}:{message_id}"] = sent
        return message_id

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        """Edit ``message_id`` in ``thread_id``, debounced per route.

        Rapid successive calls to the same ``(thread_id, message_id)``
        collapse into a single deferred write, guaranteeing at most one
        Discord API call every :data:`EDIT_THROTTLE_S` seconds. The
        most recently requested body always wins.
        """
        key = f"{thread_id}:{message_id}"
        now = time.monotonic()
        async with self._edit_lock:
            state = self._edit_state.setdefault(key, _EditState())
            state.pending_text = text
            elapsed = now - state.last_edit_ts
            if elapsed >= EDIT_THROTTLE_S and (state.task is None or state.task.done()):
                state.last_edit_ts = now
                body = state.pending_text
                state.pending_text = ""
                await self._flush_edit(thread_id, message_id, body)
                return
            if state.task is None or state.task.done():
                delay = max(0.0, EDIT_THROTTLE_S - elapsed)
                state.task = asyncio.create_task(
                    self._deferred_flush(thread_id, message_id, delay, key),
                )

    async def push_approval(self, approval: PendingApproval) -> str:
        """Render a Discord button row for ``approval``.

        Posts a message carrying the title and body plus a view with two
        buttons whose ``custom_id`` is ``approve:<id>`` / ``reject:<id>``.
        Decoding a click is symmetric: the bridge reads ``custom_id``,
        splits on ``:``, and dispatches to the registered handler.
        """
        if self._ui_mod is None:
            # ``push_approval`` is only safe to call after ``start`` -- the
            # UI module is initialised there. Mirror the Slack driver's
            # behaviour of surfacing a clear runtime error.
            raise RuntimeError(
                "DiscordBridge is not started; call await bridge.start() first.",
            )

        channel = await self._resolve_channel(approval.thread_id)
        text = f"{approval.title}\n\n{approval_body(approval)}"
        view_cls: Any = self._ui_mod.View
        button_cls: Any = self._ui_mod.Button
        style_cls: Any = getattr(self._discord_mod, "ButtonStyle", None)
        view = view_cls(timeout=None)
        approve_kwargs: dict[str, Any] = {
            "label": "Approve",
            "custom_id": f"approve:{approval.approval_id}",
        }
        reject_kwargs: dict[str, Any] = {
            "label": "Reject",
            "custom_id": f"reject:{approval.approval_id}",
        }
        if style_cls is not None:
            approve_kwargs["style"] = style_cls.success
            reject_kwargs["style"] = style_cls.danger
        view.add_item(button_cls(**approve_kwargs))
        view.add_item(button_cls(**reject_kwargs))

        envelope = self._build_signed_envelope(text)
        sent = await channel.send(text, view=view)
        self._last_signed_envelope = envelope
        message_id = str(getattr(sent, "id", "") or "")
        if message_id:
            self._sent_messages[f"{approval.thread_id}:{message_id}"] = sent
        return message_id

    # ------------------------------------------------------------------
    # Inbound dispatch -- interaction event handler
    # ------------------------------------------------------------------

    async def dispatch_interaction(self, interaction: Any) -> None:
        """Single entry point for slash commands and component clicks.

        Public so tests and operator hooks can inject synthetic
        interactions without round-tripping through the gateway. The
        gateway path calls this method internally too.
        """
        interaction_type: Any = getattr(interaction, "type", None)
        # ``InteractionType`` may be an IntEnum in the real SDK; coerce
        # to ``int`` for a robust comparison against our constants. Bail
        # on ``None`` and on anything that fails the int() cast so the
        # bridge stays forward-compatible with future Discord types.
        if interaction_type is None:
            return
        try:
            type_value = int(interaction_type)
        except (TypeError, ValueError):
            return
        if type_value == _INTERACTION_APPLICATION_COMMAND:
            await self._dispatch_slash_command(interaction)
        elif type_value == _INTERACTION_COMPONENT:
            await self._dispatch_component(interaction)
        # Unknown interaction types are dropped silently; Discord adds
        # new ones over time and the bridge is forward-compatible.

    async def _dispatch_slash_command(self, interaction: Any) -> None:
        """Route a slash-command interaction to the registered handler."""
        data: Any = getattr(interaction, "data", None)
        command_name = str(getattr(data, "name", "") or "")
        if not command_name:
            return
        handler = self._command_handlers.get(command_name.lstrip("/"))
        if handler is None:
            return
        raw_options: Any = getattr(data, "options", None) or []
        options: list[Any] = cast("list[Any]", raw_options) if isinstance(raw_options, list) else []
        args: list[str] = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            opt_dict = cast("dict[str, Any]", opt)
            args.append(f"{opt_dict.get('name')}={opt_dict.get('value')}")
        text = " ".join([command_name, *args]).strip()
        channel_id = str(getattr(interaction, "channel_id", "") or "")
        user_obj: Any = getattr(interaction, "user", None)
        user_id = str(getattr(user_obj, "id", "") or "")
        # Acknowledge the interaction so Discord stops showing the
        # "thinking" spinner. ``defer`` is the recommended path; the
        # response itself comes through the handler-controlled
        # message edits.
        await self._ack_interaction(interaction)
        await handler(
            ChatMessage(
                thread_id=channel_id,
                user_id=user_id,
                text=text,
                args=args,
                raw=interaction,
            ),
        )

    async def _dispatch_component(self, interaction: Any) -> None:
        """Route a component (button) interaction to the registered button handler.

        Decodes the ``custom_id`` ``<verb>:<approval_id>``, enforces both
        the worktree-pinning guard and the channel-partition fence, and
        chains an audit entry before invoking the registered handler.

        The audit record is the bridge's contract; it lands even if no
        button handler is registered so cross-worktree and
        cross-partition rejections plus the approval-chain entry are
        not contingent on the orchestrator wiring up an inbound callback.
        """
        data: Any = getattr(interaction, "data", None)
        custom_id = str(getattr(data, "custom_id", "") or "")
        if ":" not in custom_id:
            return
        decision, approval_id = custom_id.split(":", 1)
        if decision not in {"approve", "reject"}:
            return
        if not approval_id:
            return

        channel_id = str(getattr(interaction, "channel_id", "") or "")
        user_obj: Any = getattr(interaction, "user", None)
        user_id = str(getattr(user_obj, "id", "") or "")
        interaction_id = str(getattr(interaction, "id", "") or "")
        request_partition = partition_id_for_channel(self.platform, channel_id) if channel_id else ""

        pending = self._pending_approvals.get(approval_id)
        if pending is not None and self._worktree_id and pending.worktree_id != self._worktree_id:
            await self._ack_interaction(interaction)
            self._record_rejected_approval(
                approver=user_id,
                approval_id=approval_id,
                pending=pending,
                interaction_id=interaction_id,
                request_partition_id=request_partition,
                reason="cross_worktree",
            )
            raise CrossWorktreeApprovalError(
                f"approval {approval_id!r} bound to worktree {pending.worktree_id!r} "
                f"cannot be resolved from worktree {self._worktree_id!r}",
            )

        if pending is not None and pending.partition_id and request_partition:
            try:
                from bernstein.core.orchestration.scheduler_partitions import (
                    ChannelPartitionMap,
                )

                ChannelPartitionMap.enforce(
                    expected=pending.partition_id,
                    actual=request_partition,
                )
            except PartitionViolationError as exc:
                await self._ack_interaction(interaction)
                self._record_rejected_approval(
                    approver=user_id,
                    approval_id=approval_id,
                    pending=pending,
                    interaction_id=interaction_id,
                    request_partition_id=request_partition,
                    reason="channel_partition_mismatch",
                )
                raise ChannelPartitionMismatchError(
                    f"approval {approval_id!r} bound to partition {pending.partition_id!r} "
                    f"cannot be resolved from partition {request_partition!r}",
                ) from exc

        await self._ack_interaction(interaction)

        if pending is not None:
            self._record_resolved_approval(
                approver=user_id,
                approval_id=approval_id,
                pending=pending,
                decision=decision,
                interaction_id=interaction_id,
            )
            del self._pending_approvals[approval_id]

        if self._button_handler is not None:
            await self._button_handler(channel_id, approval_id, decision)

    # ------------------------------------------------------------------
    # Audit-chain helpers
    # ------------------------------------------------------------------

    def _record_resolved_approval(
        self,
        *,
        approver: str,
        approval_id: str,
        pending: PendingApprovalRecord,
        decision: str,
        interaction_id: str,
    ) -> None:
        """Track scheduler state and emit a chained audit entry.

        The audit log itself maintains ``prev_hmac`` (the
        ``prev_chain_digest`` from the AC); the entry body covers
        ``(approver, interaction_id, decision, tool_call_hash,
        worktree_id, partition_id)`` so a replay walker can reconstruct
        the post-approval scheduler state byte-identically.
        """
        if decision == "approve":
            self._approved_tool_call_hashes.add(pending.tool_call_hash)
            self._approved_partition_for[pending.tool_call_hash] = pending.partition_id
        elif decision == "reject":
            # A reject after a prior approval should not change the
            # approved set retroactively; only drop if the same hash had
            # been approved earlier in this session.
            self._approved_tool_call_hashes.discard(pending.tool_call_hash)
            self._approved_partition_for.pop(pending.tool_call_hash, None)
        if self._audit_log is None:
            return
        self._audit_log.log(
            event_type=APPROVAL_EVENT_TYPE,
            actor=approver or "unknown",
            resource_type="approval",
            resource_id=approval_id,
            details={
                "approver": approver,
                "decision": decision,
                "tool_call_hash": pending.tool_call_hash,
                "interaction_id": interaction_id,
                "worktree_id": pending.worktree_id,
                "partition_id": pending.partition_id,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )

    def _record_rejected_approval(
        self,
        *,
        approver: str,
        approval_id: str,
        pending: PendingApprovalRecord,
        interaction_id: str,
        request_partition_id: str,
        reason: str,
    ) -> None:
        """Log a refused approval (cross-worktree or cross-partition)."""
        if self._audit_log is None:
            return
        self._audit_log.log(
            event_type=APPROVAL_REJECTED_EVENT_TYPE,
            actor=approver or "unknown",
            resource_type="approval",
            resource_id=approval_id,
            details={
                "approver": approver,
                "reason": reason,
                "tool_call_hash": pending.tool_call_hash,
                "interaction_id": interaction_id,
                "pending_worktree_id": pending.worktree_id,
                "request_worktree_id": self._worktree_id,
                "pending_partition_id": pending.partition_id,
                "request_partition_id": request_partition_id,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )

    # ------------------------------------------------------------------
    # Throttle internals
    # ------------------------------------------------------------------

    async def _deferred_flush(
        self,
        thread_id: str,
        message_id: str,
        delay: float,
        key: str,
    ) -> None:
        """Sleep ``delay`` then flush the pending body for ``key``."""
        await asyncio.sleep(delay)
        async with self._edit_lock:
            state = self._edit_state.get(key)
            if state is None or not state.pending_text:
                return
            body = state.pending_text
            state.pending_text = ""
            state.last_edit_ts = time.monotonic()
        await self._flush_edit(thread_id, message_id, body)

    async def _flush_edit(self, thread_id: str, message_id: str, text: str) -> None:
        """Issue the actual edit API call via the recorded message handle."""
        handle: Any = self._sent_messages.get(f"{thread_id}:{message_id}")
        if handle is None:
            # The bridge may have restarted between send and edit; fall
            # back to the client's message fetcher when available.
            channel: Any = await self._resolve_channel(thread_id)
            fetcher: Any = getattr(channel, "fetch_message", None)
            if callable(fetcher):
                with contextlib.suppress(Exception):
                    lookup_id: Any = int(message_id) if message_id.isdigit() else message_id
                    fetch_result: Any = fetcher(lookup_id)
                    if hasattr(fetch_result, "__await__"):
                        handle = await fetch_result
                    else:
                        handle = fetch_result
        if handle is None:
            logger.warning(
                "discord edit dropped for %s:%s -- no message handle on record",
                thread_id,
                message_id,
            )
            return
        edit_call: Any = getattr(handle, "edit", None)
        if not callable(edit_call):
            logger.warning(
                "discord edit dropped for %s:%s -- handle has no edit() method",
                thread_id,
                message_id,
            )
            return
        try:
            result: Any = edit_call(content=text)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # pragma: no cover - network-only path.
            logger.warning("discord edit failed for %s:%s: %s", thread_id, message_id, exc)

    async def _resolve_channel(self, thread_id: str) -> Any:
        """Return a usable channel handle for ``thread_id``.

        Prefers ``client.get_channel`` (cached); falls back to
        ``fetch_channel`` for channels the cache has not seen yet.
        """
        client: Any = self._require_client()
        channel_id_value: Any
        try:
            channel_id_value = int(thread_id)
        except (TypeError, ValueError):
            channel_id_value = thread_id
        getter: Any = getattr(client, "get_channel", None)
        channel: Any = None
        if callable(getter):
            channel = getter(channel_id_value)
        if channel is None:
            fetcher: Any = getattr(client, "fetch_channel", None)
            if callable(fetcher):
                fetch_result: Any = fetcher(channel_id_value)
                if hasattr(fetch_result, "__await__"):
                    channel = await fetch_result
                else:
                    channel = fetch_result
        if channel is None:
            raise RuntimeError(f"discord channel {thread_id!r} could not be resolved.")
        return channel

    async def _ack_interaction(self, interaction: Any) -> None:
        """Acknowledge the interaction so Discord stops the thinking spinner."""
        response: Any = getattr(interaction, "response", None)
        if response is None:
            return
        defer: Any = getattr(response, "defer", None)
        if not callable(defer):
            return
        with contextlib.suppress(Exception):
            result: Any = defer()
            if hasattr(result, "__await__"):
                await result

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError(
                "DiscordBridge is not started; call await bridge.start() first.",
            )
        return self._client

    # ------------------------------------------------------------------
    # Signing helpers
    # ------------------------------------------------------------------

    def _ensure_keypair(self) -> None:
        """Load or generate the install's Ed25519 keypair on demand."""
        if self._private_key_pem is not None and self._public_key_pem is not None:
            return
        key_dir = self._key_dir or Path.cwd() / ".bernstein" / "keys" / "discord"
        key_dir.mkdir(parents=True, exist_ok=True)
        priv_path = key_dir / "discord-bridge.ed25519"
        pub_path = key_dir / "discord-bridge.ed25519.pub"

        if priv_path.exists() and pub_path.exists():
            self._private_key_pem = priv_path.read_bytes()
            self._public_key_pem = pub_path.read_bytes()
            return

        priv = Ed25519PrivateKey.generate()
        self._private_key_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._public_key_pem = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # Best-effort persist for restarts; failures here are non-fatal
        # because the in-memory copy stays authoritative for this run.
        try:
            priv_path.write_bytes(self._private_key_pem)
            priv_path.chmod(0o600)
            pub_path.write_bytes(self._public_key_pem)
        except OSError as exc:  # pragma: no cover - filesystem flake
            logger.warning(
                "could not persist discord bridge keypair under %s: %s",
                key_dir,
                exc,
            )

    def _build_signed_envelope(self, content: str) -> dict[str, str]:
        """Return the signed envelope identifying this install.

        The signed payload covers ``(install_id, session_id, content_hash)``
        per the AC. The signature is base64-encoded so it survives the
        JSON round-trip recipients use to verify it.
        """
        self._ensure_keypair()
        assert self._private_key_pem is not None
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        priv = serialization.load_pem_private_key(self._private_key_pem, password=None)
        if not isinstance(priv, Ed25519PrivateKey):  # pragma: no cover - we just generated it
            raise TypeError("expected Ed25519 private key")
        message = _canonical_attestation_bytes(self._install_id, self._session_id, content_hash)
        signature = priv.sign(message)
        return {
            "install_id": self._install_id,
            "session_id": self._session_id,
            "content_hash": content_hash,
            "signature": base64.b64encode(signature).decode("ascii"),
        }


# ---------------------------------------------------------------------------
# Public verifier
# ---------------------------------------------------------------------------


def verify_chat_signature(
    *,
    install_id: str,
    session_id: str,
    content: str,
    signature: str,
    public_key_pem: bytes,
) -> bool:
    """Return ``True`` iff ``signature`` was minted over the canonical bytes.

    Returns ``False`` on any cryptographic mismatch, malformed signature,
    or wrong public key. Never raises on bad input -- the verifier
    surface is operator-facing and must not propagate adversarial
    parsing errors.
    """
    try:
        pub = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError):
        return False
    if not isinstance(pub, Ed25519PublicKey):
        return False
    try:
        sig_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error):
        return False
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    message = _canonical_attestation_bytes(install_id, session_id, content_hash)
    try:
        pub.verify(sig_bytes, message)
    except InvalidSignature:
        return False
    return True


def _canonical_attestation_bytes(install_id: str, session_id: str, content_hash: str) -> bytes:
    """Canonical signing bytes -- stable across Python releases and locales."""
    return json.dumps(
        {
            "install_id": install_id,
            "session_id": session_id,
            "content_hash": content_hash,
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Import helpers -- keep the SDK optional.
# ---------------------------------------------------------------------------


def _import_discord() -> Any:
    try:
        return importlib.import_module("discord")
    except ImportError as exc:
        raise DiscordDependencyError(
            "discord.py is not installed. Install with: pip install 'bernstein[discord]'",
        ) from exc


def _import_discord_ui() -> Any:
    try:
        return importlib.import_module("discord.ui")
    except ImportError as exc:
        raise DiscordDependencyError(
            "discord.py is not installed. Install with: pip install 'bernstein[discord]'",
        ) from exc
