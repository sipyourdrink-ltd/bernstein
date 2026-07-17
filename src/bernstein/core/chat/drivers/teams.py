"""Microsoft Teams driver -- attested approvals over the Bot Framework.

Issue #2511. Operators whose org standardises on Microsoft Teams get the same
attested approval surface as Slack, Discord, and Telegram: interactive
Adaptive Cards, HMAC-chained approval events, worktree pinning, and Ed25519
outbound message signing.

Standard Teams bot integration: register a bot in the Azure Bot Service,
configure the Microsoft App id and password, and set ``BERNSTEIN_TEAMS_TOKEN``
(the app id) plus ``BERNSTEIN_TEAMS_APP_PASSWORD`` (the client secret).
``pip install 'bernstein[teams]'`` pulls in the Bot Framework SDK.

The SDK import is guarded so the module can always be imported --
``botbuilder-core`` is only required when :meth:`TeamsBridge.start` actually
runs. This keeps ``bernstein chat serve --platform=slack`` working for users
who only installed the Slack extra.

Key behaviours mirror the Slack driver:

  * **Commands.** A Teams message activity whose text names a registered
    subcommand routes to the matching :meth:`on_command` handler.
  * **Approval cards.** :meth:`push_approval` renders a Teams-native Adaptive
    Card with two ``Action.Submit`` buttons. When the approval carries a v2
    envelope the card body is the verbatim projection of the hashed envelope
    (no driver-local re-summarisation), so what is displayed is exactly what
    was hashed.
  * **Card actions.** An ``invoke`` / submit activity carries
    ``value = {"action": "approve"|"reject", "approval_id": ...}``; decoding
    is symmetric.
  * **Edit throttle.** :meth:`edit_message` is debounced per conversation to
    stay inside the Bot Framework's per-conversation update budget.
  * **Attested approvals.** Every card action that resolves a pending approval
    is appended to the HMAC-chained audit log as a ``chat.teams.approval``
    event covering ``(approver, activity_id, decision, tool_call_hash,
    worktree_id)``.
  * **Worktree pinning.** Approvals carry a ``worktree_id`` so a resolve for a
    worker bound to ``wt-a`` cannot settle a pending approval registered
    against a different worktree; cross-worktree attempts log a
    ``chat.teams.approval_rejected`` entry.
  * **Outbound message signing.** Every outbound message carries an Ed25519
    detached signature over ``(install_id, session_id, content_hash)`` so a
    recipient with the install's public key can confirm authenticity. The
    signature format is identical to the Slack driver's, so the shared
    :func:`verify_chat_signature` verifies both.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import logging
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.chat.bridge import (
    BridgeProtocol,
    ButtonHandler,
    ChatMessage,
    CommandHandler,
    PendingApproval,
    approval_body,
)

# The signature verifier is shared with the Slack driver: both drivers sign
# the identical canonical envelope, so one verifier covers both surfaces.
from bernstein.core.chat.drivers.slack import verify_chat_signature

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditLog

__all__ = [
    "APPROVAL_EVENT_TYPE",
    "APPROVAL_REJECTED_EVENT_TYPE",
    "EDIT_THROTTLE_S",
    "CrossWorktreeApprovalError",
    "PendingApprovalRecord",
    "TeamsBridge",
    "TeamsDependencyError",
    "verify_chat_signature",
]

logger = logging.getLogger(__name__)

#: Minimum seconds between consecutive edits to the same conversation.
EDIT_THROTTLE_S: float = 1.0

#: Audit-chain event type for a Teams approval that resolved cleanly.
APPROVAL_EVENT_TYPE: str = "chat.teams.approval"

#: Audit-chain event type for a Teams approval refused by the worktree guard.
APPROVAL_REJECTED_EVENT_TYPE: str = "chat.teams.approval_rejected"

#: Adaptive Card schema version rendered by :meth:`TeamsBridge.push_approval`.
ADAPTIVE_CARD_VERSION: str = "1.4"

#: Adaptive Card content type used for the message attachment.
ADAPTIVE_CARD_CONTENT_TYPE: str = "application/vnd.microsoft.card.adaptive"


class TeamsDependencyError(RuntimeError):
    """Raised when the Bot Framework SDK is not installed."""


class CrossWorktreeApprovalError(RuntimeError):
    """Raised when a resolve settles a pending approval on a different worktree.

    The bridge logs the rejection into the audit chain before raising so
    operators can audit attempted bypasses.
    """


@dataclass(slots=True)
class _EditState:
    """Per-conversation debouncing bookkeeping."""

    last_edit_ts: float = 0.0
    pending_text: str = ""
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass(slots=True, frozen=True)
class PendingApprovalRecord:
    """Server-side bookkeeping for a pending Teams approval."""

    approval_id: str
    tool_call_hash: str
    worktree_id: str
    thread_id: str


class TeamsBridge(BridgeProtocol):
    """Microsoft Teams implementation of :class:`BridgeProtocol`.

    Args:
        token: Microsoft App id of the registered bot. Used to authenticate
            outbound Bot Framework calls. Must be non-empty.
        app_password: Microsoft App password / client secret. Must be
            non-empty for :meth:`start` to succeed.
        install_id: Stable identifier for this Bernstein install; bound into
            the signed envelope on every outbound message.
        session_id: Stable identifier for the active chat session.
        worktree_id: Worktree this driver instance is bound to; the
            approval-resolution path refuses to settle a pending approval whose
            ``worktree_id`` differs.
        audit_log: Optional :class:`AuditLog`. When set, every resolution lands
            as a chained ``chat.teams.approval`` entry and every cross-worktree
            attempt as a ``chat.teams.approval_rejected`` entry.
        key_dir: Filesystem directory backing the install's Ed25519 keypair.
            Defaults to ``<workdir>/.bernstein/keys/teams`` when unset.
        service_url: Optional Bot Framework service url override forwarded to
            the transport when replies target a specific tenant endpoint.
    """

    platform: str = "teams"

    def __init__(
        self,
        token: str,
        app_password: str,
        *,
        install_id: str = "",
        session_id: str = "",
        worktree_id: str = "",
        audit_log: AuditLog | None = None,
        key_dir: Path | None = None,
        service_url: str = "",
    ) -> None:
        if not token:
            raise ValueError("Teams app id (token) must be non-empty.")
        if not app_password:
            raise ValueError("Teams app password must be non-empty.")

        self._token = token
        self._app_password = app_password
        self._install_id = install_id
        self._session_id = session_id
        self._worktree_id = worktree_id
        self._audit_log = audit_log
        self._service_url = service_url

        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handler: ButtonHandler | None = None

        self._client: Any = None

        self._edit_state: dict[str, _EditState] = {}
        self._edit_lock = asyncio.Lock()

        self._pending_approvals: dict[str, PendingApprovalRecord] = {}
        self._approved_tool_call_hashes: set[str] = set()

        self._key_dir = key_dir
        self._private_key_pem: bytes | None = None
        self._public_key_pem: bytes | None = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on_command(self, name: str, handler: CommandHandler) -> None:
        """Register ``handler`` for the subcommand ``<name>``."""
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
        """Tell the bridge a tool call is waiting for a Teams approval."""
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
        """Construct the Bot Framework connector client and begin serving."""
        connector_mod = _import_teams_connector()
        credentials_mod = _import_teams_credentials()
        credentials = credentials_mod.MicrosoftAppCredentials(self._token, self._app_password)
        # ``ConnectorClient`` targets a tenant service url; when the operator
        # did not pin one we defer to the SDK default so replies still route.
        self._client = connector_mod.ConnectorClient(credentials, base_url=self._service_url or None)

    async def stop(self) -> None:
        """Flush pending edits and release the connector client."""
        async with self._edit_lock:
            for state in self._edit_state.values():
                task = state.task
                if task is not None and not task.done():
                    task.cancel()
            self._edit_state.clear()
        self._client = None

    # ------------------------------------------------------------------
    # Outbound primitives
    # ------------------------------------------------------------------

    async def send_message(self, thread_id: str, text: str) -> str:
        """Post ``text`` to ``thread_id`` and return the new activity id.

        Outbound messages carry a signed envelope so a recipient with the
        install's public key can confirm the message originated here.
        """
        client = self._require_client()
        activity = {
            "type": "message",
            "text": text,
            "channelData": {"bernstein": self._build_signed_metadata(text)},
        }
        result: Any = await _maybe_await(client.send_activity(thread_id, activity))
        return _activity_id(result)

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        """Edit ``message_id`` in ``thread_id``, debounced per conversation."""
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
        """Render a Teams-native Adaptive Card for ``approval``.

        The card body is the verbatim projection of the hashed v2 envelope
        (falling back to the legacy free-text body), and the two
        ``Action.Submit`` buttons carry ``{"action", "approval_id"}`` so the
        decode path can recover the decision.
        """
        client = self._require_client()
        body_text = approval_body(approval)
        text = f"{approval.title}\n\n{body_text}"
        card = self._adaptive_card(approval.title, body_text, approval.approval_id)
        activity = {
            "type": "message",
            "text": text,
            "attachments": [
                {
                    "contentType": ADAPTIVE_CARD_CONTENT_TYPE,
                    "content": card,
                },
            ],
            "channelData": {"bernstein": self._build_signed_metadata(text)},
        }
        result: Any = await _maybe_await(client.send_activity(approval.thread_id, activity))
        return _activity_id(result)

    def _adaptive_card(self, title: str, body_text: str, approval_id: str) -> dict[str, Any]:
        """Build the Adaptive Card payload for an approval."""
        return {
            "type": "AdaptiveCard",
            "version": ADAPTIVE_CARD_VERSION,
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": body_text, "wrap": True},
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "Approve",
                    "data": {"action": "approve", "approval_id": approval_id},
                },
                {
                    "type": "Action.Submit",
                    "title": "Reject",
                    "data": {"action": "reject", "approval_id": approval_id},
                },
            ],
        }

    # ------------------------------------------------------------------
    # Inbound dispatch -- activity handler
    # ------------------------------------------------------------------

    async def handle_activity(self, activity: dict[str, Any]) -> None:
        """Public entry point for an inbound Teams activity.

        Message activities route to slash-style command handlers; card submit
        actions (message activities carrying a ``value`` with an ``action``, or
        ``invoke`` activities) route to the approval-button path.
        """
        await self._handle_activity(activity)

    async def _handle_activity(self, activity: dict[str, Any]) -> None:
        value_any: Any = activity.get("value")
        value: dict[str, Any] = cast("dict[str, Any]", value_any) if isinstance(value_any, dict) else {}
        if value.get("action") in {"approve", "reject"}:
            await self._dispatch_card_action(activity, value)
            return
        if activity.get("type") == "message":
            await self._dispatch_message(activity)

    async def _dispatch_message(self, activity: dict[str, Any]) -> None:
        """Route a message activity to the matching registered handler."""
        text = str(activity.get("text") or "").strip()
        if not text:
            return
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
        thread_id = _conversation_id(activity)
        user_id = _from_id(activity)
        await handler(
            ChatMessage(
                thread_id=thread_id,
                user_id=user_id,
                text=text,
                args=text.split()[1:],
                raw=activity,
            ),
        )

    async def _dispatch_card_action(self, activity: dict[str, Any], value: dict[str, Any]) -> None:
        """Route a card submit action to the registered button handler.

        Enforces worktree pinning and writes a chained audit entry before
        invoking the handler; the audit record lands even if no button handler
        is registered so cross-worktree rejection and the approval-chain entry
        do not depend on the orchestrator wiring an inbound callback.
        """
        decision = str(value.get("action") or "")
        if decision not in {"approve", "reject"}:
            return
        approval_id = str(value.get("approval_id") or "")
        if not approval_id:
            return
        thread_id = _conversation_id(activity)
        user_id = _from_id(activity)
        activity_id = str(activity.get("id") or activity.get("replyToId") or "")

        pending = self._pending_approvals.get(approval_id)
        if pending is not None and self._worktree_id and pending.worktree_id != self._worktree_id:
            self._record_rejected_approval(
                approver=user_id,
                approval_id=approval_id,
                pending=pending,
                activity_id=activity_id,
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
                decision=decision,
                activity_id=activity_id,
            )
            del self._pending_approvals[approval_id]

        if self._button_handler is not None:
            await self._button_handler(thread_id, approval_id, decision)

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
        activity_id: str,
    ) -> None:
        """Track scheduler state and emit a chained approval audit entry."""
        if decision == "approve":
            self._approved_tool_call_hashes.add(pending.tool_call_hash)
        elif decision == "reject":
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
                "activity_id": activity_id,
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
        activity_id: str,
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
                "activity_id": activity_id,
                "pending_worktree_id": pending.worktree_id,
                "request_worktree_id": self._worktree_id,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )

    # ------------------------------------------------------------------
    # Throttle internals
    # ------------------------------------------------------------------

    async def _deferred_flush(self, thread_id: str, message_id: str, delay: float, key: str) -> None:
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
        """Issue the actual update-activity API call."""
        client = self._require_client()
        activity = {
            "type": "message",
            "id": message_id,
            "text": text,
            "channelData": {"bernstein": self._build_signed_metadata(text)},
        }
        try:
            await _maybe_await(client.update_activity(thread_id, message_id, activity))
        except Exception as exc:  # pragma: no cover - network-only path.
            logger.warning("teams edit failed for %s:%s: %s", thread_id, message_id, exc)

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("TeamsBridge is not started; call await bridge.start() first.")
        return self._client

    # ------------------------------------------------------------------
    # Signing helpers (envelope identical to the Slack driver)
    # ------------------------------------------------------------------

    def _ensure_keypair(self) -> None:
        """Load or generate the install's Ed25519 keypair on demand."""
        if self._private_key_pem is not None and self._public_key_pem is not None:
            return
        key_dir = self._key_dir or Path.cwd() / ".bernstein" / "keys" / "teams"
        key_dir.mkdir(parents=True, exist_ok=True)
        priv_path = key_dir / "teams-bridge.ed25519"
        pub_path = key_dir / "teams-bridge.ed25519.pub"

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
        try:
            priv_path.write_bytes(self._private_key_pem)
            priv_path.chmod(0o600)
            pub_path.write_bytes(self._public_key_pem)
        except OSError as exc:  # pragma: no cover - filesystem flake
            logger.warning("could not persist teams bridge keypair under %s: %s", key_dir, exc)

    def _build_signed_metadata(self, content: str) -> dict[str, Any]:
        """Return the signed envelope embedded in ``channelData``.

        The signed payload covers ``(install_id, session_id, content_hash)``
        using the same canonical bytes the Slack driver signs, so the shared
        :func:`verify_chat_signature` verifies both surfaces.
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
# Helpers
# ---------------------------------------------------------------------------


def _canonical_attestation_bytes(install_id: str, session_id: str, content_hash: str) -> bytes:
    """Canonical signing bytes -- identical to the Slack driver's envelope."""
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


async def _maybe_await(result: Any) -> Any:
    """Return ``result``, awaiting it first when it is awaitable."""
    if hasattr(result, "__await__"):
        return await result
    return result


def _activity_id(result: Any) -> str:
    """Extract the activity/resource id from a send/update result."""
    if isinstance(result, dict):
        mapping: dict[str, Any] = cast("dict[str, Any]", result)
        return str(mapping.get("id") or mapping.get("activityId") or "")
    return str(getattr(result, "id", "") or getattr(result, "activity_id", "") or "")


def _nested_id(activity: dict[str, Any], nested_key: str, flat_key: str) -> str:
    """Return ``activity[nested_key]['id']`` or the flat fallback ``activity[flat_key]``."""
    nested_any: Any = activity.get(nested_key)
    if isinstance(nested_any, dict):
        nested: dict[str, Any] = cast("dict[str, Any]", nested_any)
        return str(nested.get("id") or "")
    return str(activity.get(flat_key) or "")


def _conversation_id(activity: dict[str, Any]) -> str:
    """Return the conversation id from an inbound activity."""
    return _nested_id(activity, "conversation", "conversation_id")


def _from_id(activity: dict[str, Any]) -> str:
    """Return the sender id from an inbound activity."""
    return _nested_id(activity, "from", "from_id")


# ---------------------------------------------------------------------------
# Import helpers -- keep the SDK optional.
# ---------------------------------------------------------------------------


def _import_teams_connector() -> Any:
    try:
        return importlib.import_module("botframework.connector")
    except ImportError as exc:
        raise TeamsDependencyError(
            "botbuilder-core is not installed. Install with: pip install 'bernstein[teams]'",
        ) from exc


def _import_teams_credentials() -> Any:
    try:
        return importlib.import_module("botframework.connector.auth")
    except ImportError as exc:
        raise TeamsDependencyError(
            "botbuilder-core is not installed. Install with: pip install 'bernstein[teams]'",
        ) from exc
