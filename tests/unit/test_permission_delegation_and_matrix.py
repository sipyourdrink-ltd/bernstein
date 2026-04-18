"""Tests for permission delegation and permission matrix."""

from __future__ import annotations

import time

from bernstein.core.permission_delegation import (
    PermissionDelegator,
    should_delegate,
)
from bernstein.core.permission_matrix import (
    PermissionRequest,
    PermissionResolutionMatrix,
    ResolutionOutcome,
    ResolutionResult,
    resolve_permission,
)


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
