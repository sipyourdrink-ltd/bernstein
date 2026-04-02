"""Tests for mailbox, permission delegation, and permission matrix."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.mailbox import MailboxMessage, MailboxSystem, MailboxQueue
from bernstein.core.permission_delegation import (
    DelegationToken,
    PermissionDelegator,
    should_delegate,
)
from bernstein.core.permission_matrix import (
    HookOutcome,
    PermissionResolutionMatrix,
    PermissionRequest,
    ResolutionOutcome,
    ResolutionResult,
    RuleOutcome,
    log_resolution,
    resolve_permission,
)


class TestMailboxSystem:
    """Test MailboxSystem functionality."""

    def test_send_and_receive(self, tmp_path: Path) -> None:
        """Test sending and receiving messages."""
        mailbox = MailboxSystem(tmp_path)

        msg_id = mailbox.send(
            sender_id="agent-1",
            recipient_id="agent-2",
            subject="Test",
            content="Hello from agent 1",
        )

        assert msg_id is not None

        messages = mailbox.receive("agent-2")

        assert len(messages) == 1
        assert messages[0].subject == "Test"
        assert messages[0].content == "Hello from agent 1"

    def test_priority_ordering(self, tmp_path: Path) -> None:
        """Test messages are ordered by priority."""
        mailbox = MailboxSystem(tmp_path)

        # Send in reverse priority order
        mailbox.send("agent-1", "agent-2", "Low", "Low priority", priority="low")
        mailbox.send("agent-1", "agent-2", "High", "High priority", priority="high")
        mailbox.send("agent-1", "agent-2", "Normal", "Normal priority", priority="normal")

        messages = mailbox.receive("agent-2")

        assert len(messages) == 3
        assert messages[0].priority == "high"
        assert messages[1].priority == "normal"
        assert messages[2].priority == "low"

    def test_message_ttl(self, tmp_path: Path) -> None:
        """Test message expiration."""
        mailbox = MailboxSystem(tmp_path, default_ttl_seconds=0.1)

        mailbox.send("agent-1", "agent-2", "Test", "Content")

        # Wait for expiration
        time.sleep(0.2)

        messages = mailbox.receive("agent-2")

        assert len(messages) == 0

    def test_unread_count(self, tmp_path: Path) -> None:
        """Test unread message count."""
        mailbox = MailboxSystem(tmp_path)

        mailbox.send("agent-1", "agent-2", "Test 1", "Content 1")
        mailbox.send("agent-1", "agent-2", "Test 2", "Content 2")

        assert mailbox.unread_count("agent-2") == 2

        mailbox.receive("agent-2")

        assert mailbox.unread_count("agent-2") == 0

    def test_cleanup_agent(self, tmp_path: Path) -> None:
        """Test agent mailbox cleanup."""
        mailbox = MailboxSystem(tmp_path)

        mailbox.send("agent-1", "agent-2", "Test", "Content")

        count = mailbox.cleanup_agent("agent-2")

        assert count == 1
        assert mailbox.unread_count("agent-2") == 0


class TestMailboxQueue:
    """Test MailboxQueue functionality."""

    def test_add_message(self) -> None:
        """Test adding messages to queue."""
        queue = MailboxQueue(agent_id="test-agent")

        msg = MailboxMessage(
            id="msg-1",
            sender_id="sender",
            recipient_id="test-agent",
            subject="Test",
            content="Content",
        )

        added = queue.add(msg)

        assert added is True
        assert len(queue.messages) == 1

    def test_queue_max_size(self) -> None:
        """Test queue respects max size."""
        queue = MailboxQueue(agent_id="test-agent", max_size=2)

        for i in range(3):
            msg = MailboxMessage(
                id=f"msg-{i}",
                sender_id="sender",
                recipient_id="test-agent",
                subject="Test",
                content="Content",
            )
            queue.add(msg)

        assert len(queue.messages) == 2

    def test_peek_doesnt_mark_read(self) -> None:
        """Test peek doesn't mark messages as read."""
        queue = MailboxQueue(agent_id="test-agent")

        msg = MailboxMessage(
            id="msg-1",
            sender_id="sender",
            recipient_id="test-agent",
            subject="Test",
            content="Content",
        )
        queue.add(msg)

        messages = queue.peek()

        assert len(messages) == 1
        assert messages[0].read is False

    def test_receive_marks_read(self) -> None:
        """Test receive marks messages as read."""
        queue = MailboxQueue(agent_id="test-agent")

        msg = MailboxMessage(
            id="msg-1",
            sender_id="sender",
            recipient_id="test-agent",
            subject="Test",
            content="Content",
        )
        queue.add(msg)

        messages = queue.receive()

        assert len(messages) == 1
        assert messages[0].read is True


class TestPermissionDelegator:
    """Test PermissionDelegator functionality."""

    def test_create_delegation(self) -> None:
        """Test creating delegation token."""
        delegator = PermissionDelegator()
        delegator.register_approval(
            approval_id="approval-1",
            scope="write",
            permissions=["read", "write"],
        )

        token = delegator.create_delegation(
            parent_approval_id="approval-1",
            coordinator_id="coord-1",
            worker_id="worker-1",
        )

        assert token is not None
        assert token.worker_id == "worker-1"
        assert token.scope == "write"

    def test_delegation_scope_hierarchy(self) -> None:
        """Test scope hierarchy validation."""
        delegator = PermissionDelegator()
        delegator.register_approval("approval-1", "write", ["read", "write"])

        # Can delegate same or lower scope
        token = delegator.create_delegation("approval-1", "coord-1", "worker-1", scope="read")
        assert token is not None

        # Cannot delegate higher scope
        token = delegator.create_delegation("approval-1", "coord-1", "worker-1", scope="full")
        assert token is None

    def test_token_expiry(self) -> None:
        """Test token expiration."""
        delegator = PermissionDelegator(default_ttl_seconds=0.1)
        delegator.register_approval("approval-1", "write", ["read"])

        token = delegator.create_delegation("approval-1", "coord-1", "worker-1")

        assert token is not None
        assert token.can_use() is True

        time.sleep(0.2)

        assert token.can_use() is False

    def test_token_max_uses(self) -> None:
        """Test token max uses limit."""
        delegator = PermissionDelegator(max_uses_per_token=2)
        delegator.register_approval("approval-1", "write", ["read"])

        token = delegator.create_delegation("approval-1", "coord-1", "worker-1")

        assert token.use() is True
        assert token.use() is True
        assert token.use() is False  # Exhausted

    def test_verify_token(self) -> None:
        """Test token verification."""
        delegator = PermissionDelegator()
        delegator.register_approval("approval-1", "write", ["read", "write"])

        token = delegator.create_delegation("approval-1", "coord-1", "worker-1")

        assert delegator.verify_token(token.token_id, "read") is True
        assert delegator.verify_token(token.token_id, "delete") is False

    def test_revoke_token(self) -> None:
        """Test token revocation."""
        delegator = PermissionDelegator()
        delegator.register_approval("approval-1", "write", ["read"])

        token = delegator.create_delegation("approval-1", "coord-1", "worker-1")

        assert delegator.revoke_token(token.token_id) is True
        assert delegator.verify_token(token.token_id, "read") is False

    def test_should_delegate(self) -> None:
        """Test delegation decision logic."""
        assert (
            should_delegate(
                coordinator_mode=True,
                has_parent_approval=True,
                worker_scope="write",
            )
            is True
        )

        assert (
            should_delegate(
                coordinator_mode=False,
                has_parent_approval=True,
                worker_scope="write",
            )
            is False
        )

        assert (
            should_delegate(
                coordinator_mode=True,
                has_parent_approval=False,
                worker_scope="write",
            )
            is False
        )


class TestPermissionResolutionMatrix:
    """Test PermissionResolutionMatrix functionality."""

    def test_rule_deny_always_denies(self) -> None:
        """Test rule deny cannot be overridden."""
        matrix = PermissionResolutionMatrix()

        # Rule deny + hook allow → deny
        result = matrix.resolve_simple("deny", "allow")
        assert result == ResolutionOutcome.DENY

        # Rule deny + hook deny → deny
        result = matrix.resolve_simple("deny", "deny")
        assert result == ResolutionOutcome.DENY

    def test_rule_ask_always_asks(self) -> None:
        """Test rule ask cannot be bypassed by hooks."""
        matrix = PermissionResolutionMatrix()

        # Rule ask + hook allow → ask
        result = matrix.resolve_simple("ask", "allow")
        assert result == ResolutionOutcome.ASK

        # Rule ask + hook deny → ask
        result = matrix.resolve_simple("ask", "deny")
        assert result == ResolutionOutcome.ASK

    def test_rule_allow_hook_deny_denies(self) -> None:
        """Test hook can restrict rule allow."""
        matrix = PermissionResolutionMatrix()

        result = matrix.resolve_simple("allow", "deny")
        assert result == ResolutionOutcome.DENY

    def test_rule_allow_hook_allow_allows(self) -> None:
        """Test rule and hook both allow."""
        matrix = PermissionResolutionMatrix()

        result = matrix.resolve_simple("allow", "allow")
        assert result == ResolutionOutcome.ALLOW

    def test_no_rule_hook_deny_denies(self) -> None:
        """Test hook deny without rule."""
        matrix = PermissionResolutionMatrix()

        result = matrix.resolve_simple(None, "deny")
        assert result == ResolutionOutcome.DENY

    def test_no_rule_hook_allow_allows(self) -> None:
        """Test hook allow without rule."""
        matrix = PermissionResolutionMatrix()

        result = matrix.resolve_simple(None, "allow")
        assert result == ResolutionOutcome.ALLOW

    def test_no_rule_hook_neutral_asks(self) -> None:
        """Test default to ask when no rule or hook decision."""
        matrix = PermissionResolutionMatrix()

        result = matrix.resolve_simple(None, None)
        assert result == ResolutionOutcome.ASK

    def test_resolve_permission_convenience(self) -> None:
        """Test convenience function."""
        result = resolve_permission("allow", "allow")
        assert result == ResolutionOutcome.ALLOW

        result = resolve_permission("deny", "allow")
        assert result == ResolutionOutcome.DENY

    def test_full_resolution_result(self) -> None:
        """Test full resolution with metadata."""
        matrix = PermissionResolutionMatrix()

        request = PermissionRequest(
            hook_name="test_hook",
            action="test_action",
            context={},
            rule_outcome="allow",
            hook_outcome="deny",
        )

        result = matrix.resolve(request)

        assert isinstance(result, ResolutionResult)
        assert result.outcome == ResolutionOutcome.DENY
        assert "Hook denies" in result.reason
        assert result.metadata["source"] == "hook_deny"
