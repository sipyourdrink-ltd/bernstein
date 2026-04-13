"""Tests for ENT-002: Cluster node registration hardening (JWT auth)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from bernstein.core.cluster_auth import (
    SCOPE_NODE_ADMIN,
    SCOPE_NODE_HEARTBEAT,
    SCOPE_NODE_REGISTER,
    AuthenticatedNodeRegistry,
    ClusterAuthConfig,
    ClusterAuthenticator,
    ClusterAuthError,
)
from bernstein.core.models import NodeCapacity, NodeInfo, NodeStatus


@pytest.fixture()
def auth_config() -> ClusterAuthConfig:
    return ClusterAuthConfig(secret="test-secret-key-for-cluster-auth")  # NOSONAR


@pytest.fixture()
def authenticator(auth_config: ClusterAuthConfig) -> ClusterAuthenticator:
    return ClusterAuthenticator(auth_config)


@pytest.fixture()
def no_auth_config() -> ClusterAuthConfig:
    return ClusterAuthConfig(secret="unused", require_auth=False)


@pytest.fixture()
def node() -> NodeInfo:
    return NodeInfo(
        id="node-1",
        name="worker-1",
        url="http://worker-1:8053",
        capacity=NodeCapacity(max_agents=4),
        status=NodeStatus.ONLINE,
        last_heartbeat=time.time(),
        registered_at=time.time(),
    )


class TestClusterAuthConfig:
    """Test auth config defaults."""

    def test_defaults(self) -> None:
        cfg = ClusterAuthConfig(secret="s")
        assert cfg.require_auth is True
        assert cfg.token_expiry_hours == 24
        assert SCOPE_NODE_REGISTER in cfg.allowed_scopes


class TestClusterAuthenticator:
    """Test JWT-based cluster authenticator."""

    def test_issue_and_verify_token(self, authenticator: ClusterAuthenticator) -> None:
        token = authenticator.issue_node_token("node-1")
        assert isinstance(token, str)
        assert len(token) > 0

        payload = authenticator.verify_request(f"Bearer {token}", SCOPE_NODE_REGISTER)
        assert payload.user_id == "node-1"
        assert SCOPE_NODE_REGISTER in payload.scopes

    def test_missing_auth_header(self, authenticator: ClusterAuthenticator) -> None:
        with pytest.raises(ClusterAuthError, match="Missing"):
            authenticator.verify_request(None)

    def test_invalid_auth_format(self, authenticator: ClusterAuthenticator) -> None:
        with pytest.raises(ClusterAuthError, match="Invalid Authorization"):
            authenticator.verify_request("Token xyz")

    def test_invalid_token(self, authenticator: ClusterAuthenticator) -> None:
        with pytest.raises(ClusterAuthError, match="Invalid or expired"):
            authenticator.verify_request("Bearer invalid.token.here")

    def test_wrong_scope(self, authenticator: ClusterAuthenticator) -> None:
        token = authenticator.issue_node_token("node-1", scopes=[SCOPE_NODE_HEARTBEAT])
        with pytest.raises(ClusterAuthError, match="lacks required scope"):
            authenticator.verify_request(f"Bearer {token}", SCOPE_NODE_ADMIN)

    def test_revoke_token(self, authenticator: ClusterAuthenticator) -> None:
        token = authenticator.issue_node_token("node-1")
        authenticator.revoke_token(token)
        with pytest.raises(ClusterAuthError, match="revoked"):
            authenticator.verify_request(f"Bearer {token}")

    def test_verify_registration(self, authenticator: ClusterAuthenticator) -> None:
        token = authenticator.issue_node_token("node-1")
        payload = authenticator.verify_registration(f"Bearer {token}")
        assert payload.user_id == "node-1"

    def test_verify_heartbeat(self, authenticator: ClusterAuthenticator) -> None:
        token = authenticator.issue_node_token("node-1")
        payload = authenticator.verify_heartbeat(f"Bearer {token}")
        assert SCOPE_NODE_HEARTBEAT in payload.scopes

    def test_is_node_authenticated(self, authenticator: ClusterAuthenticator) -> None:
        assert not authenticator.is_node_authenticated("node-1")
        authenticator.issue_node_token("node-1")
        assert authenticator.is_node_authenticated("node-1")

    def test_revoke_node(self, authenticator: ClusterAuthenticator) -> None:
        authenticator.issue_node_token("node-1")
        assert authenticator.is_node_authenticated("node-1")
        authenticator.revoke_node("node-1")
        assert not authenticator.is_node_authenticated("node-1")

    def test_auth_disabled(self, no_auth_config: ClusterAuthConfig) -> None:
        auth = ClusterAuthenticator(no_auth_config)
        payload = auth.verify_request(None)
        assert payload.session_id == "anonymous"


class TestAuthenticatedNodeRegistry:
    """Test the authenticated registry wrapper."""

    def test_register_with_valid_token(self, authenticator: ClusterAuthenticator, node: NodeInfo) -> None:
        mock_registry = MagicMock()
        mock_registry.register.return_value = node
        wrapper = AuthenticatedNodeRegistry(mock_registry, authenticator)

        token = authenticator.issue_node_token("bootstrap")
        registered, new_token = wrapper.register(node, f"Bearer {token}")
        assert registered.id == "node-1"
        assert isinstance(new_token, str)
        mock_registry.register.assert_called_once_with(node)

    def test_register_without_token(self, authenticator: ClusterAuthenticator, node: NodeInfo) -> None:
        mock_registry = MagicMock()
        wrapper = AuthenticatedNodeRegistry(mock_registry, authenticator)
        with pytest.raises(ClusterAuthError):
            wrapper.register(node, None)

    def test_heartbeat_with_valid_token(self, authenticator: ClusterAuthenticator) -> None:
        mock_registry = MagicMock()
        mock_registry.heartbeat.return_value = MagicMock()
        wrapper = AuthenticatedNodeRegistry(mock_registry, authenticator)

        token = authenticator.issue_node_token("node-1")
        result = wrapper.heartbeat("node-1", f"Bearer {token}")
        assert result is not None
        mock_registry.heartbeat.assert_called_once()

    def test_heartbeat_token_mismatch(self, authenticator: ClusterAuthenticator) -> None:
        mock_registry = MagicMock()
        wrapper = AuthenticatedNodeRegistry(mock_registry, authenticator)

        token = authenticator.issue_node_token("node-1")
        with pytest.raises(ClusterAuthError, match="mismatch"):
            wrapper.heartbeat("node-2", f"Bearer {token}")

    def test_unregister_requires_admin(self, authenticator: ClusterAuthenticator) -> None:
        mock_registry = MagicMock()
        mock_registry.unregister.return_value = True
        wrapper = AuthenticatedNodeRegistry(mock_registry, authenticator)

        # Regular token lacks admin scope
        token = authenticator.issue_node_token("node-1")
        with pytest.raises(ClusterAuthError, match="lacks required scope"):
            wrapper.unregister("node-1", f"Bearer {token}")

    def test_unregister_with_admin_token(self, authenticator: ClusterAuthenticator) -> None:
        mock_registry = MagicMock()
        mock_registry.unregister.return_value = True
        wrapper = AuthenticatedNodeRegistry(mock_registry, authenticator)

        token = authenticator.issue_node_token("admin", scopes=[SCOPE_NODE_ADMIN])
        removed = wrapper.unregister("node-1", f"Bearer {token}")
        assert removed is True

    def test_auth_disabled_allows_everything(self, no_auth_config: ClusterAuthConfig, node: NodeInfo) -> None:
        auth = ClusterAuthenticator(no_auth_config)
        mock_registry = MagicMock()
        mock_registry.register.return_value = node
        wrapper = AuthenticatedNodeRegistry(mock_registry, auth)

        registered, _token = wrapper.register(node, None)
        assert registered.id == "node-1"
