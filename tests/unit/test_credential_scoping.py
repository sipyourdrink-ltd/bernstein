"""Tests for bernstein.core.credential_scoping.

Covers credential creation, scope validation, revocation, role defaults,
and the CredentialScopeManager lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bernstein.core.credential_scoping import (
    CredentialScope,
    CredentialScopeManager,
    ScopedCredential,
    create_scoped_credential,
    get_scope_for_role,
    validate_request_against_scope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_scope() -> CredentialScope:
    """Create a backend-style scope."""
    return CredentialScope(
        allowed_operations=("code_gen", "file_read", "file_write"),
        allowed_models=("gpt-4",),
        max_tokens_per_request=8192,
        rate_limit_rpm=60,
    )


@pytest.fixture
def manager() -> CredentialScopeManager:
    """Create a fresh CredentialScopeManager."""
    return CredentialScopeManager()


# ---------------------------------------------------------------------------
# CredentialScope
# ---------------------------------------------------------------------------


class TestCredentialScope:
    """Tests for the CredentialScope dataclass."""

    def test_frozen(self) -> None:
        scope = CredentialScope(allowed_operations=("code_gen",))
        with pytest.raises(AttributeError):
            scope.allowed_operations = ("web_search",)  # type: ignore[misc]

    def test_defaults(self) -> None:
        scope = CredentialScope(allowed_operations=("code_gen",))
        assert scope.allowed_models is None
        assert scope.max_tokens_per_request is None
        assert scope.rate_limit_rpm is None

    def test_all_fields(self) -> None:
        scope = CredentialScope(
            allowed_operations=("code_gen",),
            allowed_models=("gpt-4", "claude-sonnet-4-20250514"),
            max_tokens_per_request=4096,
            rate_limit_rpm=30,
        )
        assert len(scope.allowed_operations) == 1
        assert len(scope.allowed_models) == 2  # type: ignore[arg-type]
        assert scope.max_tokens_per_request == 4096
        assert scope.rate_limit_rpm == 30


# ---------------------------------------------------------------------------
# create_scoped_credential
# ---------------------------------------------------------------------------


class TestCreateScopedCredential:
    """Tests for the create_scoped_credential helper."""

    def test_creates_credential(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        assert isinstance(cred, ScopedCredential)
        assert cred.agent_id == "agent-1"
        assert cred.scope is backend_scope

    def test_key_id_format(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        assert cred.key_id.startswith("sk-")
        assert len(cred.key_id) > 5

    def test_unique_key_ids(self, backend_scope: CredentialScope) -> None:
        cred_a = create_scoped_credential("agent-1", backend_scope)
        cred_b = create_scoped_credential("agent-1", backend_scope)
        assert cred_a.key_id != cred_b.key_id

    def test_expiry_default_ttl(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        delta = cred.expires_at - cred.created_at
        assert delta == timedelta(hours=24)

    def test_expiry_custom_ttl(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope, ttl_hours=1)
        delta = cred.expires_at - cred.created_at
        assert delta == timedelta(hours=1)

    def test_created_at_is_utc(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        assert cred.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# validate_request_against_scope
# ---------------------------------------------------------------------------


class TestValidateRequestAgainstScope:
    """Tests for request-scope validation."""

    def test_valid_operation(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope(
            {"operation": "code_gen"}, backend_scope
        )

    def test_invalid_operation(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope(
            {"operation": "web_search"}, backend_scope
        )

    def test_valid_model(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope(
            {"model": "gpt-4"}, backend_scope
        )

    def test_invalid_model(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope(
            {"model": "llama-3"}, backend_scope
        )

    def test_no_model_restriction(self) -> None:
        scope = CredentialScope(allowed_operations=("code_gen",))
        assert validate_request_against_scope(
            {"model": "anything"}, scope
        )

    def test_tokens_within_budget(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope(
            {"tokens": 100}, backend_scope
        )

    def test_tokens_exceeds_budget(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope(
            {"tokens": 10000}, backend_scope
        )

    def test_empty_request(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope({}, backend_scope)

    def test_combined_fields(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope(
            {"operation": "code_gen", "model": "gpt-4", "tokens": 100},
            backend_scope,
        )

    def test_combined_one_invalid(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope(
            {"operation": "code_gen", "model": "llama-3", "tokens": 100},
            backend_scope,
        )


# ---------------------------------------------------------------------------
# get_scope_for_role
# ---------------------------------------------------------------------------


class TestGetScopeForRole:
    """Tests for role-to-scope mapping."""

    def test_backend_role(self) -> None:
        scope = get_scope_for_role("backend")
        assert "code_gen" in scope.allowed_operations
        assert scope.allowed_models is not None

    def test_researcher_role(self) -> None:
        scope = get_scope_for_role("researcher")
        assert "web_search" in scope.allowed_operations
        assert "code_gen" not in scope.allowed_operations

    def test_admin_role(self) -> None:
        scope = get_scope_for_role("admin")
        assert "system_admin" in scope.allowed_operations

    def test_unknown_role_gets_minimal(self) -> None:
        scope = get_scope_for_role("nonexistent")
        assert scope.allowed_operations == ("file_read",)
        assert scope.max_tokens_per_request == 1024

    def test_frontend_role(self) -> None:
        scope = get_scope_for_role("frontend")
        assert "code_gen" in scope.allowed_operations
        assert "web_search" not in scope.allowed_operations


# ---------------------------------------------------------------------------
# CredentialScopeManager
# ---------------------------------------------------------------------------


class TestCredentialScopeManager:
    """Tests for the CredentialScopeManager."""

    def test_create_and_get(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        fetched = manager.get(cred.key_id)
        assert fetched is cred

    def test_is_valid_fresh(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        assert manager.is_valid(cred.key_id)

    def test_is_valid_unknown_key(self, manager: CredentialScopeManager) -> None:
        assert not manager.is_valid("sk-nonexistent")

    def test_revoke(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        manager.revoke(cred.key_id)
        assert not manager.is_valid(cred.key_id)

    def test_revoke_all_for_agent(self, manager: CredentialScopeManager) -> None:
        manager.create("agent-1", get_scope_for_role("backend"))
        manager.create("agent-1", get_scope_for_role("frontend"))
        manager.create("agent-2", get_scope_for_role("backend"))
        count = manager.revoke_all_for_agent("agent-1")
        assert count == 2

    def test_list_for_agent(self, manager: CredentialScopeManager) -> None:
        manager.create("agent-1", get_scope_for_role("backend"))
        manager.create("agent-1", get_scope_for_role("frontend"))
        manager.create("agent-2", get_scope_for_role("backend"))
        creds = manager.list_for_agent("agent-1")
        assert len(creds) == 2

    def test_list_excludes_revoked(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        manager.revoke(cred.key_id)
        assert len(manager.list_for_agent("agent-1")) == 0

    def test_validate_request(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        assert manager.validate_request(
            cred.key_id, {"operation": "code_gen", "model": "gpt-4"}
        )

    def test_validate_request_revoked(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        manager.revoke(cred.key_id)
        assert not manager.validate_request(
            cred.key_id, {"operation": "code_gen"}
        )

    def test_validate_request_out_of_scope(
        self, manager: CredentialScopeManager
    ) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        assert not manager.validate_request(
            cred.key_id, {"operation": "web_search"}
        )

    def test_cleanup_expired(self, manager: CredentialScopeManager) -> None:
        # Create a credential that expires immediately
        cred = manager.create(
            "agent-1", get_scope_for_role("backend"), ttl_hours=0
        )
        # Manually set expiration in the past
        expired_cred = ScopedCredential(
            key_id=cred.key_id,
            agent_id=cred.agent_id,
            scope=cred.scope,
            created_at=cred.created_at,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        manager._credentials[cred.key_id] = expired_cred
        removed = manager.cleanup_expired()
        assert removed == 1
        assert manager.get(cred.key_id) is None
