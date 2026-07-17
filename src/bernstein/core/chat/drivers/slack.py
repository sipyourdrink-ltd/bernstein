"""Slack driver -- bidirectional Socket Mode bridge with attested approvals.

Standard Slack bot integration: configure a bot token (``xoxb-...``) plus a
Socket Mode app token (``xapp-...``) and the bridge connects via Slack's
Socket Mode transport so the operator does not need to expose a public HTTP
endpoint. ``pip install 'bernstein[slack]'`` pulls in the ``slack-sdk``
library.

The library import is guarded so the module can always be imported -
``slack-sdk`` is only required when :meth:`SlackBridge.start` actually runs.
This keeps ``bernstein chat serve --platform=telegram`` working for users
who only installed the Telegram extra.

Key behaviours:

  * **Slash commands.** Handlers registered via :meth:`on_command` are
    routed based on the leading subcommand token in the slash payload.
    The raw :class:`~bernstein.core.chat.bridge.ChatMessage` is forwarded
    so handlers can read the remaining argv. The driver normalises the
    payload across the Socket Mode and HTTP slash-command shapes.
  * **Approval buttons.** :meth:`push_approval` renders a Block Kit
    ``actions`` block with two buttons whose ``action_id`` is either
    ``approve`` or ``reject`` and whose ``value`` carries the approval
    id. Decoding a button press is symmetric: read ``action_id`` and
    ``value`` and call the registered handler.
  * **Edit throttle.** :meth:`edit_message` is debounced to one edit
    per thread per :data:`EDIT_THROTTLE_S` seconds. Slack's per-channel
    rate limit (``chat.update``: ~1 message/sec/channel) will otherwise
    kill streaming agent output mid-burst.
  * **Attested approvals.** Every Slack button press that resolves a
    pending approval is appended to the HMAC-chained audit log as a
    ``chat.slack.approval`` event whose ``details`` cover
    ``(approver, message_ts, decision, tool_call_hash, worktree_id)``;
    the chain's ``prev_hmac`` provides the ``prev_chain_digest`` link.
  * **Worktree pinning.** Approvals carry a ``worktree_id`` so an
    ``/approve`` for a worker bound to ``wt-a`` cannot resolve a pending
    approval registered against a different worktree. Cross-worktree
    attempts log a ``chat.slack.approval_rejected`` audit entry so the
    bypass is visible to the operator.
  * **Outbound message signing.** Every outbound chat message carries an
    Ed25519 detached signature over ``(install_id, session_id,
    content_hash)``. The bridge mints (or reuses) the install's Ed25519
    keypair via :class:`AgentCardKeystore` so a recipient with the
    install's public key can verify the message was not injected by
    another bernstein install impersonating the workspace.
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
import shlex
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

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditLog

__all__ = [
    "APPROVAL_EVENT_TYPE",
    "APPROVAL_REJECTED_EVENT_TYPE",
    "EDIT_THROTTLE_S",
    "CrossWorktreeApprovalError",
    "PendingApprovalRecord",
    "SlackBridge",
    "SlackDependencyError",
    "verify_chat_signature",
]

logger = logging.getLogger(__name__)

#: Minimum seconds between consecutive edits to the same (channel, ts). Slack
#: throttles ``chat.update`` at roughly one message per second per channel;
#: leaving a comfortable margin keeps streaming agents inside the limit.
EDIT_THROTTLE_S: float = 1.0

#: Audit-chain event type for an approval that resolved cleanly.
APPROVAL_EVENT_TYPE: str = "chat.slack.approval"

#: Audit-chain event type for an approval that was rejected by the
#: worktree-pinning guard (or any other operator-visible refusal).
APPROVAL_REJECTED_EVENT_TYPE: str = "chat.slack.approval_rejected"

#: Default slash command prefix surfaced by the Slack app. Operators map
#: ``/bernstein`` in their Slack app config, then route subcommands via
#: :meth:`SlackBridge.on_command`.
DEFAULT_SLASH_COMMAND: str = "/bernstein"

#: Slack ``metadata.event_type`` value used for signed outbound messages.
SIGNED_METADATA_EVENT_TYPE: str = "bernstein.attested_message"


class SlackDependencyError(RuntimeError):
    """Raised when ``slack-sdk`` is not installed."""


class CrossWorktreeApprovalError(RuntimeError):
    """Raised when an /approve resolves a pending approval on a different worktree.

    The bridge logs the rejection into the audit chain before raising so
    operators can audit attempted bypasses.
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
    """Server-side bookkeeping for a pending approval.

    The driver stores one of these per approval-id so resolution-time
    handlers can enforce worktree pinning and emit the chained audit entry
    with the tool-call digest the approval covers.
    """

    approval_id: str
    tool_call_hash: str
    worktree_id: str
    thread_id: str


class SlackBridge(BridgeProtocol):
    """Slack implementation of :class:`BridgeProtocol` over Socket Mode.

    Args:
        token: Bot token (``xoxb-...``). Used for ``chat.postMessage`` and
            ``chat.update`` against the Slack Web API.
        app_token: Socket Mode app token (``xapp-...``). Used to open the
            WebSocket that delivers slash commands and block-action
            payloads. Must be non-empty for :meth:`start` to succeed.
        install_id: Stable identifier for this Bernstein install. Embedded
            in the signed envelope on every outbound message so a recipient
            with the install's public key can confirm authenticity.
        session_id: Stable identifier for the active chat session. Bound
            into the signed envelope alongside ``install_id``.
        worktree_id: Identifier of the worktree this driver instance is
            bound to. The approval-resolution path refuses to settle any
            pending approval whose ``worktree_id`` differs.
        audit_log: Optional :class:`AuditLog`. When set, every approval
            resolution lands as a chained ``chat.slack.approval`` entry
            and every rejected cross-worktree attempt as a
            ``chat.slack.approval_rejected`` entry.
        key_dir: Filesystem directory backing the install's Ed25519
            keypair. Defaults to ``<workdir>/.bernstein/keys/slack`` when
            unset. Two bridges constructed with distinct ``key_dir`` will
            sign with distinct keys, mirroring two separate installs.
    """

    platform: str = "slack"

    def __init__(
        self,
        token: str,
        app_token: str,
        *,
        install_id: str = "",
        session_id: str = "",
        worktree_id: str = "",
        audit_log: AuditLog | None = None,
        key_dir: Path | None = None,
    ) -> None:
        if not token:
            raise ValueError("Slack bot token must be non-empty.")
        if not app_token:
            raise ValueError("Slack app-level token must be non-empty.")

        self._token = token
        self._app_token = app_token
        self._install_id = install_id
        self._session_id = session_id
        self._worktree_id = worktree_id
        self._audit_log = audit_log

        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handler: ButtonHandler | None = None

        self._web: Any = None
        self._socket: Any = None
        self._sdk_response_cls: Any = None

        self._edit_state: dict[str, _EditState] = {}
        self._edit_lock = asyncio.Lock()

        # Pending approvals registered by the orchestrator. Keyed by
        # approval_id so the resolution path can look the record up
        # without scanning the dict.
        self._pending_approvals: dict[str, PendingApprovalRecord] = {}
        # Resolved approvals -- public surface for replay / scheduler-state
        # reconstruction during tests and operator audit.
        self._approved_tool_call_hashes: set[str] = set()

        # Lazy keypair so importing this module never touches the filesystem.
        self._key_dir = key_dir
        self._private_key_pem: bytes | None = None
        self._public_key_pem: bytes | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on_command(self, name: str, handler: CommandHandler) -> None:
        """Register ``handler`` for the slash subcommand ``<name>``."""
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
    ) -> None:
        """Tell the bridge a tool call is waiting for a Slack approval.

        The resolution path uses ``worktree_id`` to enforce worktree
        pinning and ``tool_call_hash`` to populate the audit entry that
        gets chained when the approver clicks Approve.
        """
        self._pending_approvals[approval_id] = PendingApprovalRecord(
            approval_id=approval_id,
            tool_call_hash=tool_call_hash,
            worktree_id=worktree_id,
            thread_id=thread_id,
        )

    def approved_tool_call_hashes(self) -> set[str]:
        """Return a snapshot of every tool-call hash that has been approved."""
        return self._approved_tool_call_hashes.copy()

    def public_key_pem(self) -> bytes:
        """Return the install's Ed25519 public key (PEM, SubjectPublicKeyInfo)."""
        self._ensure_keypair()
        assert self._public_key_pem is not None
        return self._public_key_pem

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Slack via Socket Mode and begin dispatching events."""
        web_mod = _import_slack_async_web()
        socket_mod = _import_slack_socket_mode()
        response_mod = _import_slack_socket_response()

        self._web = web_mod.AsyncWebClient(token=self._token)
        self._socket = socket_mod.SocketModeClient(
            app_token=self._app_token,
            web_client=self._web,
        )
        self._sdk_response_cls = response_mod.SocketModeResponse

        # ``socket_mode_request_listeners`` is a list attribute on the real
        # ``SocketModeClient`` (slack-sdk >= 3.10). Mutate it in place so
        # the SDK delivers slash + interactive envelopes to us.
        listeners_attr: Any = getattr(self._socket, "socket_mode_request_listeners", None)
        listeners: list[Any]
        if isinstance(listeners_attr, list):
            listeners = cast("list[Any]", listeners_attr)
        elif callable(listeners_attr):
            # Some shims (and our test fake) expose the registry through a
            # zero-arg method that returns the underlying list.
            listeners = cast("list[Any]", listeners_attr())
        else:  # pragma: no cover - defensive against future SDK shape changes
            listeners = []
        listeners.append(self._handle_socket_mode_request)

        await self._socket.connect()

    async def stop(self) -> None:
        """Flush pending edits and disconnect cleanly."""
        async with self._edit_lock:
            for state in self._edit_state.values():
                task = state.task
                if task is not None and not task.done():
                    task.cancel()
            self._edit_state.clear()

        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.disconnect()
            close: Any = getattr(self._socket, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    result: Any = close()
                    if hasattr(result, "__await__"):
                        await result
            self._socket = None
        self._web = None

    # ------------------------------------------------------------------
    # Outbound primitives
    # ------------------------------------------------------------------

    async def send_message(self, thread_id: str, text: str) -> str:
        """Post ``text`` to ``thread_id`` and return the new message ts.

        Outbound messages carry a signed metadata envelope so a recipient
        with the install's public key can confirm the message originated
        from this workspace and was not injected by another install.
        """
        client = self._require_web()
        metadata = self._build_signed_metadata(text)
        response = await client.chat_postMessage(
            channel=thread_id,
            text=text,
            metadata=metadata,
        )
        return str(response.get("ts", ""))

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        """Edit ``message_id`` in ``thread_id``, debounced per channel.

        Rapid successive calls to the same ``(thread_id, message_id)``
        collapse into a single deferred write, guaranteeing at most one
        Slack API call every :data:`EDIT_THROTTLE_S` seconds. The most
        recently requested body always wins.
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
        """Render a Block Kit approval card for ``approval``.

        Posts a header section plus a context section carrying the body
        text, then an ``actions`` block with two buttons whose
        ``action_id`` is ``approve`` / ``reject`` and whose ``value``
        carries the approval id.
        """
        client = self._require_web()
        body = approval_body(approval)
        text = f"{approval.title}\n\n{body}"
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{approval.title}*"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            },
            {
                "type": "actions",
                "block_id": f"approval_{approval.approval_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "value": approval.approval_id,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                    },
                    {
                        "type": "button",
                        "action_id": "reject",
                        "value": approval.approval_id,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                    },
                ],
            },
        ]
        metadata = self._build_signed_metadata(text)
        response = await client.chat_postMessage(
            channel=approval.thread_id,
            text=text,
            blocks=blocks,
            metadata=metadata,
        )
        return str(response.get("ts", ""))

    # ------------------------------------------------------------------
    # Inbound dispatch -- Socket Mode handler
    # ------------------------------------------------------------------

    async def _handle_socket_mode_request(self, *args: Any) -> None:
        """Single entry point for slash commands and interactive payloads.

        The real ``slack_sdk`` socket-mode handler is called with
        ``(client, request)``; some test shims call it with just the
        request. We accept either.
        """
        request: Any
        if len(args) == 2:
            _client, request = args
        elif len(args) == 1:
            request = args[0]
        else:  # pragma: no cover - defensive
            return

        await self._ack(request)
        req_type = str(getattr(request, "type", "") or "")
        payload = getattr(request, "payload", {}) or {}

        if req_type in {"slash_commands", "slash_command"}:
            await self._dispatch_slash_command(payload)
        elif req_type in {"interactive", "block_actions"} or payload.get("type") == "block_actions":
            await self._dispatch_block_action(payload)
        # Unknown envelope types are dropped silently; the operator's app
        # configuration may evolve to deliver shapes this version does not
        # yet decode.

    async def _ack(self, request: Any) -> None:
        """Acknowledge the Socket Mode envelope when the SDK exposes the API."""
        send_socket_mode_response: Any = getattr(self._socket, "send_socket_mode_response", None)
        if not callable(send_socket_mode_response):
            return
        envelope_id = getattr(request, "envelope_id", "")
        if not envelope_id or self._sdk_response_cls is None:  # pragma: no cover - defensive
            return
        response = self._sdk_response_cls(envelope_id=envelope_id)
        result: Any = send_socket_mode_response(response)
        if hasattr(result, "__await__"):
            await result

    async def _dispatch_slash_command(self, payload: dict[str, Any]) -> None:
        """Route a slash-command payload to the matching registered handler.

        Slack delivers the bare command (e.g. ``/bernstein``) plus the
        remainder as ``text``. We split the remainder via ``shlex`` so
        operators can quote arguments and read ``args`` from the
        :class:`ChatMessage` without re-parsing.
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        # ``shlex.split`` honours quoting; fall back to a whitespace split if
        # the operator typed unbalanced quotes (Slack accepts those clientside).
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            tokens = text.split()
        if not tokens:
            return
        subcommand = tokens[0].lstrip("/")
        handler = self._command_handlers.get(subcommand)
        if handler is None:
            return

        thread_id = str(payload.get("channel_id") or payload.get("channel", {}).get("id") or "")
        user_id = str(payload.get("user_id") or payload.get("user", {}).get("id") or "")
        await handler(
            ChatMessage(
                thread_id=thread_id,
                user_id=user_id,
                text=text,
                # Preserve the original text-split semantics from the
                # Telegram driver: callers get the same whitespace-split
                # arg list, not the shlex-tokenised one.
                args=text.split()[1:],
                raw=payload,
            ),
        )

    async def _dispatch_block_action(self, payload: dict[str, Any]) -> None:
        """Route a ``block_actions`` payload to the registered button handler.

        Decodes ``action_id`` ∈ ``{approve, reject}`` and ``value`` as the
        approval id. When a pending approval is registered for that id, the
        bridge enforces worktree pinning and writes a chained audit entry
        before invoking the registered handler.

        Note: the audit record is the bridge's contract; it lands even if no
        button handler is registered, so cross-worktree rejection and the
        approval-chain entry are not contingent on the orchestrator wiring
        up an inbound callback.
        """
        actions_raw: Any = payload.get("actions")
        actions: list[Any] = cast("list[Any]", actions_raw) if isinstance(actions_raw, list) else []
        if not actions:
            return
        action_any: Any = actions[0]
        action: dict[str, Any] = cast("dict[str, Any]", action_any) if isinstance(action_any, dict) else {}
        action_id = str(action.get("action_id") or "")
        if action_id not in {"approve", "reject"}:
            return
        approval_id = str(action.get("value") or "")
        if not approval_id:
            return
        channel_any: Any = payload.get("channel")
        user_any: Any = payload.get("user")
        message_any: Any = payload.get("message")
        channel_obj: dict[str, Any] = cast("dict[str, Any]", channel_any) if isinstance(channel_any, dict) else {}
        user_obj: dict[str, Any] = cast("dict[str, Any]", user_any) if isinstance(user_any, dict) else {}
        message_obj: dict[str, Any] = cast("dict[str, Any]", message_any) if isinstance(message_any, dict) else {}
        thread_id = str(channel_obj.get("id") or "")
        user_id = str(user_obj.get("id") or "")
        message_ts = str(message_obj.get("ts") or "")

        pending = self._pending_approvals.get(approval_id)
        if pending is not None and self._worktree_id and pending.worktree_id != self._worktree_id:
            # Cross-worktree attempt -- log + refuse without invoking handler.
            self._record_rejected_approval(
                approver=user_id,
                approval_id=approval_id,
                pending=pending,
                message_ts=message_ts,
                reason="cross_worktree",
            )
            raise CrossWorktreeApprovalError(
                f"approval {approval_id!r} bound to worktree {pending.worktree_id!r} "
                f"cannot be resolved from worktree {self._worktree_id!r}",
            )

        if pending is not None:
            self._record_resolved_approval(
                approver=user_id,
                approval_id=approval_id,
                pending=pending,
                decision=action_id,
                message_ts=message_ts,
            )
            del self._pending_approvals[approval_id]

        if self._button_handler is not None:
            await self._button_handler(thread_id, approval_id, action_id)

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
        message_ts: str,
    ) -> None:
        """Track scheduler state and emit a chained audit entry.

        The audit log itself maintains ``prev_hmac`` (the
        ``prev_chain_digest`` from the AC); the entry body covers
        ``(approver, message_ts, decision, tool_call_hash, worktree_id)``
        so a replay walker can reconstruct the post-approval state.
        """
        if decision == "approve":
            self._approved_tool_call_hashes.add(pending.tool_call_hash)
        elif decision == "reject":
            # A reject after a prior approval should not change the
            # approved set retroactively; only drop if the same hash had
            # been (incorrectly) approved earlier in this session.
            self._approved_tool_call_hashes.discard(pending.tool_call_hash)
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
                "message_ts": message_ts,
                "worktree_id": pending.worktree_id,
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
        message_ts: str,
        reason: str,
    ) -> None:
        """Log a rejected cross-worktree approval into the audit chain."""
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
                "message_ts": message_ts,
                "pending_worktree_id": pending.worktree_id,
                "request_worktree_id": self._worktree_id,
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
        """Issue the actual ``chat.update`` API call."""
        client = self._require_web()
        try:
            await client.chat_update(
                channel=thread_id,
                ts=message_id,
                text=text,
            )
        except Exception as exc:  # pragma: no cover - network-only path.
            logger.warning("slack edit failed for %s:%s: %s", thread_id, message_id, exc)

    def _require_web(self) -> Any:
        if self._web is None:
            raise RuntimeError(
                "SlackBridge is not started; call await bridge.start() first.",
            )
        return self._web

    # ------------------------------------------------------------------
    # Signing helpers
    # ------------------------------------------------------------------

    def _ensure_keypair(self) -> None:
        """Load or generate the install's Ed25519 keypair on demand."""
        if self._private_key_pem is not None and self._public_key_pem is not None:
            return
        key_dir = self._key_dir or Path.cwd() / ".bernstein" / "keys" / "slack"
        key_dir.mkdir(parents=True, exist_ok=True)
        priv_path = key_dir / "slack-bridge.ed25519"
        pub_path = key_dir / "slack-bridge.ed25519.pub"

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
        # because the in-memory copy is still authoritative for this run.
        try:
            priv_path.write_bytes(self._private_key_pem)
            priv_path.chmod(0o600)
            pub_path.write_bytes(self._public_key_pem)
        except OSError as exc:  # pragma: no cover - filesystem flake
            logger.warning("could not persist slack bridge keypair under %s: %s", key_dir, exc)

    def _build_signed_metadata(self, content: str) -> dict[str, Any]:
        """Return the Slack ``metadata`` envelope with an Ed25519 signature.

        The signed payload covers ``(install_id, session_id, content_hash)``
        per the AC. The signature is base64-encoded so it survives the
        JSON round-trip Slack performs on the metadata field.
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
            "event_type": SIGNED_METADATA_EVENT_TYPE,
            "event_payload": {
                "install_id": self._install_id,
                "session_id": self._session_id,
                "content_hash": content_hash,
                "signature": base64.b64encode(signature).decode("ascii"),
            },
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
    """Return ``True`` iff ``signature`` was minted over ``(install_id, session_id, content_hash)``.

    Returns ``False`` on any cryptographic mismatch, malformed signature,
    or wrong public key. Never raises on bad input -- the verifier surface
    is operator-facing and must not propagate adversarial parsing errors.
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


def _import_slack_async_web() -> Any:
    try:
        return importlib.import_module("slack_sdk.web.async_client")
    except ImportError as exc:
        raise SlackDependencyError(
            "slack-sdk is not installed. Install with: pip install 'bernstein[slack]'",
        ) from exc


def _import_slack_socket_mode() -> Any:
    try:
        return importlib.import_module("slack_sdk.socket_mode.aiohttp")
    except ImportError as exc:
        raise SlackDependencyError(
            "slack-sdk is not installed. Install with: pip install 'bernstein[slack]'",
        ) from exc


def _import_slack_socket_response() -> Any:
    try:
        return importlib.import_module("slack_sdk.socket_mode.response")
    except ImportError as exc:
        raise SlackDependencyError(
            "slack-sdk is not installed. Install with: pip install 'bernstein[slack]'",
        ) from exc
