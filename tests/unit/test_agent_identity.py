"""Tests for Agent Identity Lifecycle Management."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.agent_identity import (
    AgentCredential,
    AgentIdentity,
    AgentIdentityStatus,
    AgentIdentityStore,
    IdentityAuditEvent,
    _hash_token,
    permissions_for_role,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Permission tests
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_manager_has_spawn_permission(self) -> None:
        perms = permissions_for_role("manager")
        assert "agents:spawn" in perms
        assert "tasks:write" in perms

    def test_backend_has_file_write(self) -> None:
        perms = permissions_for_role("backend")
        assert "files:write" in perms
        assert "tests:run" in perms

    def test_qa_cannot_write_files(self) -> None:
        perms = permissions_for_role("qa")
        assert "files:write" not in perms
        assert "files:read" in perms

    def test_unknown_role_gets_defaults(self) -> None:
        perms = permissions_for_role("unknown-role")
        assert "tasks:read" in perms
        assert "files:read" in perms
        assert "agents:spawn" not in perms


# ---------------------------------------------------------------------------
# AgentCredential tests
# ---------------------------------------------------------------------------


class TestAgentCredential:
    def test_valid_credential(self) -> None:
        cred = AgentCredential(token_hash="abc123")
        assert cred.is_valid

    def test_revoked_credential_invalid(self) -> None:
        cred = AgentCredential(token_hash="abc123", revoked=True)
        assert not cred.is_valid

    def test_expired_credential_invalid(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=time.time() - 100)
        assert not cred.is_valid

    def test_future_expiry_valid(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=time.time() + 3600)
        assert cred.is_valid

    def test_zero_expiry_means_no_expiry(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=0.0)
        assert cred.is_valid

    def test_roundtrip_serialization(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=99999.0, revoked=False)
        restored = AgentCredential.from_dict(cred.to_dict())
        assert restored.token_hash == cred.token_hash
        assert restored.expires_at == cred.expires_at
        assert restored.revoked == cred.revoked


# ---------------------------------------------------------------------------
# AgentIdentity tests
# ---------------------------------------------------------------------------


class TestAgentIdentity:
    def test_active_identity_has_permission(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="backend",
            session_id="test-1",
            permissions=frozenset({"files:read", "files:write"}),
        )
        assert identity.has_permission("files:read")
        assert not identity.has_permission("agents:spawn")

    def test_revoked_identity_denies_all(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="backend",
            session_id="test-1",
            permissions=frozenset({"files:read"}),
            status=AgentIdentityStatus.REVOKED,
        )
        assert not identity.has_permission("files:read")

    def test_suspended_identity_denies_all(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="backend",
            session_id="test-1",
            permissions=frozenset({"files:read"}),
            status=AgentIdentityStatus.SUSPENDED,
        )
        assert not identity.has_permission("files:read")

    def test_is_active_property(self) -> None:
        active = AgentIdentity(id="a", role="backend", session_id="a")
        revoked = AgentIdentity(id="b", role="backend", session_id="b", status=AgentIdentityStatus.REVOKED)
        assert active.is_active
        assert not revoked.is_active

    def test_roundtrip_serialization(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="security",
            session_id="test-1",
            permissions=frozenset({"files:read", "tests:run"}),
            parent_identity_id="parent-1",
            metadata={"cell_id": "cell-abc"},
            credential=AgentCredential(token_hash="hash123"),
        )
        restored = AgentIdentity.from_dict(identity.to_dict())
        assert restored.id == identity.id
        assert restored.role == identity.role
        assert restored.permissions == identity.permissions
        assert restored.parent_identity_id == "parent-1"
        assert restored.metadata == {"cell_id": "cell-abc"}
        assert restored.credential is not None
        assert restored.credential.token_hash == "hash123"

    def test_serialization_without_credential(self) -> None:
        identity = AgentIdentity(id="test-1", role="qa", session_id="test-1")
        data = identity.to_dict()
        assert data["credential"] is None
        restored = AgentIdentity.from_dict(data)
        assert restored.credential is None


# ---------------------------------------------------------------------------
# IdentityAuditEvent tests
# ---------------------------------------------------------------------------


class TestIdentityAuditEvent:
    def test_roundtrip(self) -> None:
        event = IdentityAuditEvent(
            timestamp=12345.0,
            identity_id="test-1",
            action="created",
            actor="spawner",
            details={"role": "backend"},
        )
        data = event.to_dict()
        assert data["identity_id"] == "test-1"
        assert data["action"] == "created"
        assert data["details"]["role"] == "backend"


# ---------------------------------------------------------------------------
# AgentIdentityStore tests
# ---------------------------------------------------------------------------


class TestAgentIdentityStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> AgentIdentityStore:
        return AgentIdentityStore(tmp_path)

    def test_create_identity(self, store: AgentIdentityStore) -> None:
        identity, token = store.create_identity("backend-abc123", "backend")
        assert identity.id == "backend-abc123"
        assert identity.role == "backend"
        assert identity.is_active
        assert "files:write" in identity.permissions
        assert len(token) > 0

    def test_create_with_extra_permissions(self, store: AgentIdentityStore) -> None:
        identity, _ = store.create_identity("mgr-1", "manager", extra_permissions=frozenset({"admin:override"}))
        assert "admin:override" in identity.permissions
        assert "agents:spawn" in identity.permissions

    def test_create_with_parent_identity(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager")
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id)
        assert child.parent_identity_id == "parent-1"

    def test_create_with_metadata(self, store: AgentIdentityStore) -> None:
        identity, _ = store.create_identity("s-1", "backend", metadata={"cell_id": "cell-x", "provider": "claude"})
        assert identity.metadata["cell_id"] == "cell-x"

    def test_authenticate_valid_token(self, store: AgentIdentityStore) -> None:
        _, token = store.create_identity("backend-abc", "backend")
        authed = store.authenticate(token)
        assert authed is not None
        assert authed.id == "backend-abc"
        assert authed.last_authenticated_at > 0

    def test_authenticate_invalid_token(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert store.authenticate("bad-token") is None

    def test_authenticate_revoked_identity(self, store: AgentIdentityStore) -> None:
        _, token = store.create_identity("backend-abc", "backend")
        store.revoke("backend-abc", reason="test")
        assert store.authenticate(token) is None

    def test_authorize_granted(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert store.authorize("backend-abc", "files:write")

    def test_authorize_denied(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert not store.authorize("backend-abc", "agents:spawn")

    def test_authorize_nonexistent(self, store: AgentIdentityStore) -> None:
        assert not store.authorize("no-such-id", "files:read")

    def test_revoke_identity(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        ok = store.revoke("backend-abc", reason="session ended")
        assert ok
        identity = store.get("backend-abc")
        assert identity is not None
        assert identity.status == AgentIdentityStatus.REVOKED
        assert identity.revoked_at > 0
        assert identity.revocation_reason == "session ended"
        assert identity.credential is not None
        assert identity.credential.revoked

    def test_revoke_nonexistent(self, store: AgentIdentityStore) -> None:
        assert not store.revoke("no-such-id")

    def test_revoke_and_suspend_logs_escape_newlines(
        self, store: AgentIdentityStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        store.create_identity("backend-abc", "backend")
        store.create_identity("backend-def", "backend")

        with caplog.at_level("INFO", logger="bernstein.core.agents.agent_identity"):
            assert store.revoke("backend-abc", reason="line1\nline2")
            assert store.suspend("backend-def", reason="line3\rline4")

        messages = [record.getMessage() for record in caplog.records if "agent_identity" in record.name]
        assert any("line1\\nline2" in message for message in messages)
        assert any("line3\\rline4" in message for message in messages)
        assert all("line1\nline2" not in message for message in messages)
        assert all("line3\rline4" not in message for message in messages)

    def test_suspend_and_reactivate(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert store.suspend("backend-abc", reason="investigation")
        identity = store.get("backend-abc")
        assert identity is not None
        assert identity.status == AgentIdentityStatus.SUSPENDED

        assert store.reactivate("backend-abc")
        identity = store.get("backend-abc")
        assert identity is not None
        assert identity.status == AgentIdentityStatus.ACTIVE

    def test_reactivate_revoked_fails(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        store.revoke("backend-abc")
        assert not store.reactivate("backend-abc")

    def test_list_all_identities(self, store: AgentIdentityStore) -> None:
        store.create_identity("a-1", "backend")
        store.create_identity("b-2", "qa")
        store.create_identity("c-3", "security")
        identities = store.list_identities()
        assert len(identities) == 3

    def test_list_by_status(self, store: AgentIdentityStore) -> None:
        store.create_identity("a-1", "backend")
        store.create_identity("b-2", "qa")
        store.revoke("b-2")
        active = store.list_identities(status=AgentIdentityStatus.ACTIVE)
        revoked = store.list_identities(status=AgentIdentityStatus.REVOKED)
        assert len(active) == 1
        assert len(revoked) == 1
        assert active[0].id == "a-1"
        assert revoked[0].id == "b-2"

    def test_list_by_role(self, store: AgentIdentityStore) -> None:
        store.create_identity("a-1", "backend")
        store.create_identity("b-2", "qa")
        result = store.list_identities(role="qa")
        assert len(result) == 1
        assert result[0].role == "qa"

    def test_get_identity(self, store: AgentIdentityStore) -> None:
        store.create_identity("test-1", "backend")
        identity = store.get("test-1")
        assert identity is not None
        assert identity.id == "test-1"

    def test_get_nonexistent(self, store: AgentIdentityStore) -> None:
        assert store.get("no-such") is None

    def test_audit_trail(self, store: AgentIdentityStore) -> None:
        store.create_identity("test-1", "backend")
        store.authorize("test-1", "files:read")
        store.revoke("test-1", reason="done")
        trail = store.get_audit_trail("test-1")
        actions = [e.action for e in trail]
        assert "created" in actions
        assert "authorized" in actions
        assert "revoked" in actions

    def test_audit_trail_limit(self, store: AgentIdentityStore) -> None:
        store.create_identity("test-1", "backend")
        for _ in range(10):
            store.authorize("test-1", "files:read")
        trail = store.get_audit_trail("test-1", limit=3)
        assert len(trail) == 3

    def test_audit_trail_empty(self, store: AgentIdentityStore) -> None:
        trail = store.get_audit_trail("no-such")
        assert trail == []

    def test_token_with_expiry(self, store: AgentIdentityStore) -> None:
        identity, token = store.create_identity("s-1", "backend", token_expiry_s=3600)
        assert identity.credential is not None
        assert identity.credential.expires_at > 0
        authed = store.authenticate(token)
        assert authed is not None

    def test_persistence_across_store_instances(self, tmp_path: Path) -> None:
        store1 = AgentIdentityStore(tmp_path)
        store1.create_identity("persist-1", "backend")

        store2 = AgentIdentityStore(tmp_path)
        identity = store2.get("persist-1")
        assert identity is not None
        assert identity.id == "persist-1"

    def test_token_index_rebuilt_on_new_store(self, tmp_path: Path) -> None:
        store1 = AgentIdentityStore(tmp_path)
        _, token = store1.create_identity("persist-1", "backend")

        store2 = AgentIdentityStore(tmp_path)
        authed = store2.authenticate(token)
        assert authed is not None
        assert authed.id == "persist-1"

    def test_corrupt_identity_file_skipped(self, tmp_path: Path) -> None:
        store = AgentIdentityStore(tmp_path)
        corrupt_path = tmp_path / "agent_identities" / "corrupt.json"
        corrupt_path.write_text("not json", encoding="utf-8")
        identities = store.list_identities()
        assert len(identities) == 0


# ---------------------------------------------------------------------------
# hash_token tests
# ---------------------------------------------------------------------------


class TestHashToken:
    def test_deterministic(self) -> None:
        h1 = _hash_token("my-secret-token")
        h2 = _hash_token("my-secret-token")
        assert h1 == h2

    def test_different_tokens_different_hashes(self) -> None:
        h1 = _hash_token("token-a")
        h2 = _hash_token("token-b")
        assert h1 != h2
