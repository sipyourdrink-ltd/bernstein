"""Unit tests for the Microsoft Teams driver (issue #2511).

The Teams driver must pass the same driver contract as Slack, Discord, and
Telegram:

* token / app-password validation
* missing-SDK error path
* command dispatch
* card-action decode (approve / reject)
* push_approval renders a Teams-native Adaptive Card with two submit actions
* edit debounce (rate-limit guard)
* outbound message signature verification
* cross-worktree approval rejection
* audit-chain entry shape for approvals
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.chat.bridge import ChatMessage, PendingApproval

# ---------------------------------------------------------------------------
# Fake botframework connector SDK
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeConnectorClient:
    """Records every outbound Bot Framework call."""

    credentials: Any = None
    base_url: str | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    counter: int = 0

    async def send_activity(self, conversation_id: str, activity: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        activity_id = f"act-{self.counter}"
        self.sent.append({"conversation_id": conversation_id, "activity": activity, "id": activity_id})
        return {"id": activity_id}

    async def update_activity(self, conversation_id: str, activity_id: str, activity: dict[str, Any]) -> dict[str, Any]:
        self.updated.append({"conversation_id": conversation_id, "activity_id": activity_id, "activity": activity})
        return {"id": activity_id}


@dataclass(slots=True)
class _FakeCredentials:
    app_id: str = ""
    app_password: str = ""


@pytest.fixture
def fake_teams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a synthetic ``botframework`` tree in ``sys.modules``."""
    connector = types.ModuleType("botframework.connector")
    connector.ConnectorClient = _FakeConnectorClient  # type: ignore[attr-defined]

    auth = types.ModuleType("botframework.connector.auth")
    auth.MicrosoftAppCredentials = _FakeCredentials  # type: ignore[attr-defined]

    connector.auth = auth  # type: ignore[attr-defined]

    pkg = types.ModuleType("botframework")
    pkg.connector = connector  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "botframework", pkg)
    monkeypatch.setitem(sys.modules, "botframework.connector", connector)
    monkeypatch.setitem(sys.modules, "botframework.connector.auth", auth)


# ---------------------------------------------------------------------------
# Constructor / token validation
# ---------------------------------------------------------------------------


def test_teams_empty_app_id_rejected() -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    with pytest.raises(ValueError):
        TeamsBridge(token="", app_password="secret")


def test_teams_empty_app_password_rejected() -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    with pytest.raises(ValueError):
        TeamsBridge(token="app-id", app_password="")


# ---------------------------------------------------------------------------
# Missing-SDK error path
# ---------------------------------------------------------------------------


def test_teams_start_without_sdk_raises_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge, TeamsDependencyError

    for modname in list(sys.modules):
        if modname == "botframework" or modname.startswith("botframework."):
            monkeypatch.delitem(sys.modules, modname, raising=False)
    monkeypatch.setitem(sys.modules, "botframework", None)  # type: ignore[arg-type]

    bridge = TeamsBridge(token="app-id", app_password="secret")
    with pytest.raises(TeamsDependencyError) as excinfo:
        asyncio.run(bridge.start())
    assert "bernstein[teams]" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def test_teams_message_routes_to_registered_handler(fake_teams: None) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    received: list[ChatMessage] = []

    async def handler(msg: ChatMessage) -> None:  # NOSONAR
        received.append(msg)

    bridge = TeamsBridge(token="app-id", app_password="secret")
    bridge.on_command("run", handler)

    async def scenario() -> None:
        await bridge.start()
        await bridge.handle_activity(_message_activity(text='run "Add JWT auth"', conversation="C42", user="U7"))
        await bridge.stop()

    asyncio.run(scenario())
    assert len(received) == 1
    assert received[0].thread_id == "C42"
    assert received[0].user_id == "U7"


def test_teams_message_ignores_unknown_subcommand(fake_teams: None) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    bridge = TeamsBridge(token="app-id", app_password="secret")

    async def scenario() -> None:
        await bridge.start()
        await bridge.handle_activity(_message_activity(text="nope", conversation="C42", user="U7"))
        await bridge.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Card-action decode (approve / reject)
# ---------------------------------------------------------------------------


def test_teams_card_action_decode_round_trip(fake_teams: None) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    decisions: list[tuple[str, str, str]] = []

    async def button(thread_id: str, approval_id: str, decision: str) -> None:  # NOSONAR
        decisions.append((thread_id, approval_id, decision))

    bridge = TeamsBridge(token="app-id", app_password="secret")
    bridge.on_button(button)

    async def scenario() -> None:
        await bridge.start()
        await bridge.handle_activity(_card_action(action="approve", approval_id="t-42", conversation="C99"))
        await bridge.handle_activity(_card_action(action="reject", approval_id="t-43", conversation="C99"))
        await bridge.stop()

    asyncio.run(scenario())
    assert decisions == [("C99", "t-42", "approve"), ("C99", "t-43", "reject")]


def test_teams_card_action_ignores_unknown_action(fake_teams: None) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    decisions: list[tuple[str, str, str]] = []

    async def button(thread_id: str, approval_id: str, decision: str) -> None:  # NOSONAR
        decisions.append((thread_id, approval_id, decision))

    bridge = TeamsBridge(token="app-id", app_password="secret")
    bridge.on_button(button)

    async def scenario() -> None:
        await bridge.start()
        await bridge.handle_activity(_card_action(action="snooze", approval_id="t-42", conversation="C99"))
        await bridge.stop()

    asyncio.run(scenario())
    assert decisions == []


# ---------------------------------------------------------------------------
# push_approval renders an Adaptive Card
# ---------------------------------------------------------------------------


def test_teams_push_approval_renders_adaptive_card(fake_teams: None) -> None:
    from bernstein.core.chat.drivers.teams import ADAPTIVE_CARD_CONTENT_TYPE, TeamsBridge

    bridge = TeamsBridge(token="app-id", app_password="secret", install_id="install-test", session_id="sess-1")

    async def scenario() -> list[dict[str, Any]]:
        await bridge.start()
        await bridge.push_approval(
            PendingApproval(
                approval_id="t-7",
                title="Approve shell command?",
                body="rm -rf /tmp/scratch",
                thread_id="C42",
            ),
        )
        sent = list(bridge._client.sent)  # type: ignore[attr-defined]
        await bridge.stop()
        return sent

    sent = asyncio.run(scenario())
    assert len(sent) == 1
    attachments = sent[0]["activity"]["attachments"]
    assert attachments[0]["contentType"] == ADAPTIVE_CARD_CONTENT_TYPE
    actions = attachments[0]["content"]["actions"]
    assert [a["data"]["action"] for a in actions] == ["approve", "reject"]
    assert [a["data"]["approval_id"] for a in actions] == ["t-7", "t-7"]


def test_teams_push_approval_renders_v2_card_verbatim(fake_teams: None) -> None:
    from bernstein.core.approval.card import build_card, card_hash, render_card_text
    from bernstein.core.chat.drivers.teams import TeamsBridge

    card = build_card(
        approval_id="t-9",
        tool_name="Bash",
        tool_args={"command": "rm -rf /var/data"},
        reasoning="Clear stale data.",
        created_at=1_000.0,
        ttl_seconds=600.0,
    )
    bridge = TeamsBridge(token="app-id", app_password="secret")

    async def scenario() -> dict[str, Any]:
        await bridge.start()
        await bridge.push_approval(
            PendingApproval(
                approval_id="t-9",
                title="Approve?",
                body="ignored-when-card-present",
                thread_id="C42",
                card=card,
                card_hash=card_hash(card),
            ),
        )
        sent = bridge._client.sent[0]  # type: ignore[attr-defined]
        await bridge.stop()
        return sent

    sent = asyncio.run(scenario())
    body_blocks = sent["activity"]["attachments"][0]["content"]["body"]
    rendered = body_blocks[1]["text"]
    # The card body is the verbatim projection of the hashed envelope.
    assert rendered == render_card_text(card)
    assert "IRREVERSIBLE" in rendered
    assert card_hash(card) in rendered


# ---------------------------------------------------------------------------
# Edit debounce
# ---------------------------------------------------------------------------


def test_teams_edit_debounce_collapses_rapid_updates(fake_teams: None) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge

    bridge = TeamsBridge(token="app-id", app_password="secret")

    async def scenario() -> list[dict[str, Any]]:
        await bridge.start()
        for i in range(5):
            await bridge.edit_message("C42", "act-1", f"tick {i}")
        edits = list(bridge._client.updated)  # type: ignore[attr-defined]
        await bridge.stop()
        return edits

    edits = asyncio.run(scenario())
    assert len(edits) == 1, f"expected a single throttled edit, got {edits}"
    assert edits[0]["activity"]["text"] == "tick 0"


# ---------------------------------------------------------------------------
# Outbound message signing
# ---------------------------------------------------------------------------


def test_teams_send_message_includes_signed_envelope(fake_teams: None, tmp_path: Path) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge, verify_chat_signature

    bridge = TeamsBridge(
        token="app-id",
        app_password="secret",
        install_id="install-A",
        session_id="sess-1",
        key_dir=tmp_path / "keys-A",
    )

    async def scenario() -> dict[str, Any]:
        await bridge.start()
        await bridge.send_message("C42", "hello operator")
        sent = bridge._client.sent[0]  # type: ignore[attr-defined]
        await bridge.stop()
        return sent

    sent = asyncio.run(scenario())
    assert "hello operator" in sent["activity"]["text"]
    payload = sent["activity"]["channelData"]["bernstein"]
    assert payload["install_id"] == "install-A"
    assert payload["session_id"] == "sess-1"
    assert verify_chat_signature(
        install_id="install-A",
        session_id="sess-1",
        content="hello operator",
        signature=payload["signature"],
        public_key_pem=bridge.public_key_pem(),
    )


def test_teams_signature_rejects_foreign_install(fake_teams: None, tmp_path: Path) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge, verify_chat_signature

    bridge_a = TeamsBridge(
        token="app-a",
        app_password="secret",
        install_id="install-A",
        session_id="sess-1",
        key_dir=tmp_path / "keys-A",
    )
    bridge_b = TeamsBridge(
        token="app-b",
        app_password="secret",
        install_id="install-B",
        session_id="sess-2",
        key_dir=tmp_path / "keys-B",
    )

    async def scenario() -> tuple[str, bytes]:
        await bridge_a.start()
        await bridge_b.start()
        await bridge_b.send_message("C42", "spoofed")
        sig = bridge_b._client.sent[0]["activity"]["channelData"]["bernstein"]["signature"]  # type: ignore[attr-defined]
        pub_a = bridge_a.public_key_pem()
        await bridge_a.stop()
        await bridge_b.stop()
        return sig, pub_a

    foreign_signature, public_key_a = asyncio.run(scenario())
    assert not verify_chat_signature(
        install_id="install-B",
        session_id="sess-2",
        content="spoofed",
        signature=foreign_signature,
        public_key_pem=public_key_a,
    )


# ---------------------------------------------------------------------------
# Audit-chain entry shape for approvals
# ---------------------------------------------------------------------------


def test_teams_approval_audit_chain_entry_shape(fake_teams: None, tmp_path: Path) -> None:
    from bernstein.core.chat.drivers.teams import TeamsBridge
    from bernstein.core.security.audit import AuditLog

    audit = AuditLog(audit_dir=tmp_path / "audit", key=b"deterministic-test-key")
    bridge = TeamsBridge(
        token="app-id",
        app_password="secret",
        install_id="install-A",
        session_id="sess-1",
        worktree_id="wt-a",
        audit_log=audit,
        key_dir=tmp_path / "keys-A",
    )

    async def scenario() -> None:
        await bridge.start()
        bridge.register_pending_approval(
            approval_id="t-7",
            tool_call_hash="hash-of-tool-call",
            worktree_id="wt-a",
            thread_id="C42",
        )
        await bridge.handle_activity(
            _card_action(action="approve", approval_id="t-7", conversation="C42", user="U7", activity_id="act-x"),
        )
        await bridge.stop()

    asyncio.run(scenario())

    entries = audit.query(event_type="chat.teams.approval")
    assert len(entries) == 1
    details = entries[0].details
    assert details["approver"] == "U7"
    assert details["decision"] == "approve"
    assert details["tool_call_hash"] == "hash-of-tool-call"
    assert details["activity_id"] == "act-x"
    assert details["worktree_id"] == "wt-a"
    valid, errors = audit.verify()
    assert valid, errors


# ---------------------------------------------------------------------------
# Worktree pinning -- cross-worktree rejection
# ---------------------------------------------------------------------------


def test_teams_cross_worktree_approval_rejected(fake_teams: None, tmp_path: Path) -> None:
    from bernstein.core.chat.drivers.teams import CrossWorktreeApprovalError, TeamsBridge
    from bernstein.core.security.audit import AuditLog

    audit = AuditLog(audit_dir=tmp_path / "audit", key=b"deterministic-test-key")
    bridge = TeamsBridge(
        token="app-id",
        app_password="secret",
        install_id="install-A",
        session_id="sess-1",
        worktree_id="wt-a",
        audit_log=audit,
        key_dir=tmp_path / "keys-A",
    )

    async def scenario() -> bool:
        await bridge.start()
        bridge.register_pending_approval(
            approval_id="t-7",
            tool_call_hash="hash-of-tool-call",
            worktree_id="wt-b",
            thread_id="C42",
        )
        try:
            await bridge.handle_activity(
                _card_action(action="approve", approval_id="t-7", conversation="C42", user="U7"),
            )
        except CrossWorktreeApprovalError:
            raised = True
        else:
            raised = False
        await bridge.stop()
        return raised

    raised = asyncio.run(scenario())
    assert raised, "approve from a different worktree must be rejected"
    assert audit.query(event_type="chat.teams.approval") == []
    rejected = audit.query(event_type="chat.teams.approval_rejected")
    assert len(rejected) == 1
    assert rejected[0].details["reason"] == "cross_worktree"


# ---------------------------------------------------------------------------
# Activity helpers
# ---------------------------------------------------------------------------


def _message_activity(*, text: str, conversation: str, user: str) -> dict[str, Any]:
    return {
        "type": "message",
        "text": text,
        "conversation": {"id": conversation},
        "from": {"id": user},
    }


def _card_action(
    *,
    action: str,
    approval_id: str,
    conversation: str,
    user: str = "U7",
    activity_id: str = "act-1",
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": activity_id,
        "conversation": {"id": conversation},
        "from": {"id": user},
        "value": {"action": action, "approval_id": approval_id},
    }
