"""Tests for SSO / SAML / OIDC authentication system."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.auth import (
    AuthRole,
    AuthService,
    AuthSession,
    AuthStore,
    AuthUser,
    DeviceAuthRequest,
    SSOConfig,
    create_jwt,
    parse_group_role_map,
    resolve_role,
    role_has_permission,
    verify_jwt,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Role & permission tests
# ---------------------------------------------------------------------------


class TestRolePermissions:
    def test_admin_has_all_permissions(self) -> None:
        assert role_has_permission(AuthRole.ADMIN, "tasks:read")
        assert role_has_permission(AuthRole.ADMIN, "tasks:write")
        assert role_has_permission(AuthRole.ADMIN, "auth:manage")
        assert role_has_permission(AuthRole.ADMIN, "config:write")

    def test_operator_has_task_permissions(self) -> None:
        assert role_has_permission(AuthRole.OPERATOR, "tasks:read")
        assert role_has_permission(AuthRole.OPERATOR, "tasks:write")
        assert role_has_permission(AuthRole.OPERATOR, "agents:kill")

    def test_operator_cannot_manage_auth(self) -> None:
        assert not role_has_permission(AuthRole.OPERATOR, "auth:manage")
        assert not role_has_permission(AuthRole.OPERATOR, "config:write")

    def test_viewer_read_only(self) -> None:
        assert role_has_permission(AuthRole.VIEWER, "tasks:read")
        assert role_has_permission(AuthRole.VIEWER, "status:read")
        assert not role_has_permission(AuthRole.VIEWER, "tasks:write")
        assert not role_has_permission(AuthRole.VIEWER, "agents:kill")


# ---------------------------------------------------------------------------
# JWT tests
# ---------------------------------------------------------------------------


class TestJWT:
    def test_create_and_verify(self) -> None:
        secret = "test-secret-key-for-jwt-testing"
        claims = {"sub": "user123", "email": "test@example.com", "role": "admin"}
        token = create_jwt(claims, secret, expiry_seconds=3600)

        result = verify_jwt(token, secret)
        assert result is not None
        assert result["sub"] == "user123"
        assert result["email"] == "test@example.com"
        assert result["role"] == "admin"
        assert "iat" in result
        assert "exp" in result
        assert "jti" in result

    def test_expired_token_rejected(self) -> None:
        secret = "test-secret"
        token = create_jwt({"sub": "user1"}, secret, expiry_seconds=-1)
        assert verify_jwt(token, secret) is None

    def test_wrong_secret_rejected(self) -> None:
        token = create_jwt({"sub": "user1"}, "secret-a")
        assert verify_jwt(token, "secret-b") is None

    def test_tampered_payload_rejected(self) -> None:
        secret = "test-secret"
        token = create_jwt({"sub": "user1"}, secret)
        # Tamper with the payload
        parts = token.split(".")
        parts[1] = parts[1] + "tampered"
        tampered = ".".join(parts)
        assert verify_jwt(tampered, secret) is None

    def test_malformed_token_rejected(self) -> None:
        assert verify_jwt("not-a-jwt", "secret") is None
        assert verify_jwt("a.b", "secret") is None
        assert verify_jwt("", "secret") is None

    def test_hs384_algorithm(self) -> None:
        secret = "test-secret"
        token = create_jwt({"sub": "user1"}, secret, algorithm="HS384")
        result = verify_jwt(token, secret, algorithm="HS384")
        assert result is not None
        assert result["sub"] == "user1"

    def test_wrong_algorithm_rejected(self) -> None:
        secret = "test-secret"
        token = create_jwt({"sub": "user1"}, secret, algorithm="HS256")
        assert verify_jwt(token, secret, algorithm="HS384") is None

    def test_unsupported_algorithm_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            create_jwt({"sub": "user1"}, "secret", algorithm="RS256")


# ---------------------------------------------------------------------------
# Group → role mapping tests
# ---------------------------------------------------------------------------


class TestGroupRoleMapping:
    def test_parse_simple_mapping(self) -> None:
        result = parse_group_role_map("admins=admin,devs=operator,everyone=viewer")
        assert result == {
            "admins": AuthRole.ADMIN,
            "devs": AuthRole.OPERATOR,
            "everyone": AuthRole.VIEWER,
        }

    def test_parse_empty_string(self) -> None:
        assert parse_group_role_map("") == {}
        assert parse_group_role_map("  ") == {}

    def test_parse_with_whitespace(self) -> None:
        result = parse_group_role_map(" admins = admin , devs = operator ")
        assert result["admins"] == AuthRole.ADMIN
        assert result["devs"] == AuthRole.OPERATOR

    def test_parse_invalid_role_skipped(self) -> None:
        result = parse_group_role_map("admins=admin,bad=superuser")
        assert len(result) == 1
        assert result["admins"] == AuthRole.ADMIN

    def test_resolve_highest_privilege(self) -> None:
        mapping = {
            "admins": AuthRole.ADMIN,
            "devs": AuthRole.OPERATOR,
            "everyone": AuthRole.VIEWER,
        }
        # User in both admins and devs → gets admin
        assert resolve_role(["admins", "devs"], mapping) == AuthRole.ADMIN

    def test_resolve_operator_over_viewer(self) -> None:
        mapping = {
            "devs": AuthRole.OPERATOR,
            "everyone": AuthRole.VIEWER,
        }
        assert resolve_role(["devs", "everyone"], mapping) == AuthRole.OPERATOR

    def test_resolve_default_role(self) -> None:
        mapping = {"admins": AuthRole.ADMIN}
        assert resolve_role(["unknown-group"], mapping) == AuthRole.VIEWER
        assert resolve_role([], mapping, AuthRole.OPERATOR) == AuthRole.OPERATOR

    def test_resolve_no_groups(self) -> None:
        assert resolve_role([], {}) == AuthRole.VIEWER


# ---------------------------------------------------------------------------
# AuthUser model tests
# ---------------------------------------------------------------------------


class TestAuthUser:
    def test_serialization_roundtrip(self) -> None:
        user = AuthUser(
            id="u123",
            email="test@example.com",
            display_name="Test User",
            role=AuthRole.OPERATOR,
            sso_provider="oidc",
            sso_subject="sub-abc",
            sso_groups=["devs", "backend"],
        )
        d = user.to_dict()
        restored = AuthUser.from_dict(d)
        assert restored.id == user.id
        assert restored.email == user.email
        assert restored.role == AuthRole.OPERATOR
        assert restored.sso_groups == ["devs", "backend"]

    def test_has_permission(self) -> None:
        admin = AuthUser(id="a", email="a@test.com", display_name="A", role=AuthRole.ADMIN)
        assert admin.has_permission("auth:manage")

        viewer = AuthUser(id="v", email="v@test.com", display_name="V", role=AuthRole.VIEWER)
        assert not viewer.has_permission("tasks:write")
        assert viewer.has_permission("tasks:read")


# ---------------------------------------------------------------------------
# AuthSession model tests
# ---------------------------------------------------------------------------


class TestAuthSession:
    def test_valid_session(self) -> None:
        session = AuthSession(user_id="u1", expires_at=time.time() + 3600)
        assert session.is_valid
        assert not session.is_expired

    def test_expired_session(self) -> None:
        session = AuthSession(user_id="u1", expires_at=time.time() - 1)
        assert session.is_expired
        assert not session.is_valid

    def test_revoked_session(self) -> None:
        session = AuthSession(user_id="u1", expires_at=time.time() + 3600, revoked=True)
        assert not session.is_valid

    def test_serialization_roundtrip(self) -> None:
        session = AuthSession(user_id="u1", expires_at=time.time() + 3600, ip_address="127.0.0.1")
        d = session.to_dict()
        restored = AuthSession.from_dict(d)
        assert restored.user_id == session.user_id
        assert restored.ip_address == "127.0.0.1"


# ---------------------------------------------------------------------------
# AuthStore tests (file-based persistence)
# ---------------------------------------------------------------------------


class TestAuthStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> AuthStore:
        return AuthStore(tmp_path)

    def test_save_and_get_user(self, store: AuthStore) -> None:
        user = AuthUser(id="u1", email="test@test.com", display_name="Test")
        store.save_user(user)
        loaded = store.get_user("u1")
        assert loaded is not None
        assert loaded.email == "test@test.com"

    def test_get_nonexistent_user(self, store: AuthStore) -> None:
        assert store.get_user("nonexistent") is None

    def test_find_user_by_email(self, store: AuthStore) -> None:
        user = AuthUser(id="u1", email="find@test.com", display_name="Find Me")
        store.save_user(user)
        found = store.find_user_by_email("find@test.com")
        assert found is not None
        assert found.id == "u1"

    def test_find_user_by_sso_subject(self, store: AuthStore) -> None:
        user = AuthUser(id="u1", email="t@t.com", display_name="T", sso_provider="oidc", sso_subject="sub-123")
        store.save_user(user)
        found = store.find_user_by_sso_subject("oidc", "sub-123")
        assert found is not None
        assert found.email == "t@t.com"

    def test_list_users(self, store: AuthStore) -> None:
        store.save_user(AuthUser(id="u1", email="a@t.com", display_name="A"))
        store.save_user(AuthUser(id="u2", email="b@t.com", display_name="B"))
        users = store.list_users()
        assert len(users) == 2

    def test_save_and_get_session(self, store: AuthStore) -> None:
        session = AuthSession(id="s1", user_id="u1", expires_at=time.time() + 3600)
        store.save_session(session)
        loaded = store.get_session("s1")
        assert loaded is not None
        assert loaded.user_id == "u1"

    def test_revoke_session(self, store: AuthStore) -> None:
        session = AuthSession(id="s1", user_id="u1", expires_at=time.time() + 3600)
        store.save_session(session)
        assert store.revoke_session("s1")
        loaded = store.get_session("s1")
        assert loaded is not None
        assert loaded.revoked

    def test_revoke_user_sessions(self, store: AuthStore) -> None:
        store.save_session(AuthSession(id="s1", user_id="u1", expires_at=time.time() + 3600))
        store.save_session(AuthSession(id="s2", user_id="u1", expires_at=time.time() + 3600))
        store.save_session(AuthSession(id="s3", user_id="u2", expires_at=time.time() + 3600))
        count = store.revoke_user_sessions("u1")
        assert count == 2

    def test_cleanup_expired_sessions(self, store: AuthStore) -> None:
        store.save_session(AuthSession(id="s1", user_id="u1", expires_at=time.time() - 1))
        store.save_session(AuthSession(id="s2", user_id="u1", expires_at=time.time() + 3600))
        removed = store.cleanup_expired_sessions()
        assert removed == 1
        assert store.get_session("s1") is None
        assert store.get_session("s2") is not None

    def test_device_request_lifecycle(self, store: AuthStore) -> None:
        req = DeviceAuthRequest()
        store.save_device_request(req)

        loaded = store.get_device_request(req.device_code)
        assert loaded is not None
        assert loaded.user_code == req.user_code
        assert not loaded.authorized

        # Authorize
        loaded.authorized = True
        loaded.user_id = "u1"
        store.save_device_request(loaded)

        found = store.find_device_by_user_code(req.user_code)
        assert found is not None
        assert found.authorized

        # Delete
        store.delete_device_request(req.device_code)
        assert store.get_device_request(req.device_code) is None

    def test_group_mappings(self, store: AuthStore) -> None:
        mappings = {
            "admins": AuthRole.ADMIN,
            "devs": AuthRole.OPERATOR,
        }
        store.save_group_mappings(mappings)
        loaded = store.load_group_mappings()
        assert loaded["admins"] == AuthRole.ADMIN
        assert loaded["devs"] == AuthRole.OPERATOR


# ---------------------------------------------------------------------------
# AuthService tests
# ---------------------------------------------------------------------------


class TestAuthService:
    @pytest.fixture()
    def svc(self, tmp_path: Path) -> AuthService:
        config = SSOConfig(
            enabled=True,
            jwt_secret="test-jwt-secret-for-unit-tests",
            jwt_expiry_seconds=3600,
            session_expiry_seconds=3600,
            default_role="viewer",
            group_role_map="admins=admin,devs=operator",
        )
        store = AuthStore(tmp_path)
        return AuthService(config, store)

    def test_group_role_map_loaded(self, svc: AuthService) -> None:
        mapping = svc.group_role_map
        assert mapping["admins"] == AuthRole.ADMIN
        assert mapping["devs"] == AuthRole.OPERATOR

    def test_upsert_user_creates_new(self, svc: AuthService) -> None:
        user = svc._upsert_user(
            provider="oidc",
            subject="sub-1",
            email="new@test.com",
            display_name="New User",
            groups=["devs"],
        )
        assert user.email == "new@test.com"
        assert user.role == AuthRole.OPERATOR  # "devs" maps to operator
        assert user.sso_provider == "oidc"

    def test_upsert_user_updates_existing(self, svc: AuthService) -> None:
        user1 = svc._upsert_user("oidc", "sub-1", "a@t.com", "A", ["devs"])
        user2 = svc._upsert_user("oidc", "sub-1", "a@t.com", "A Updated", ["admins"])
        assert user2.id == user1.id
        assert user2.display_name == "A Updated"
        assert user2.role == AuthRole.ADMIN  # Now in admins group

    def test_issue_and_validate_token(self, svc: AuthService) -> None:
        user = svc._upsert_user("oidc", "sub-1", "t@t.com", "T", [])
        token = svc._issue_token(user)

        result = svc.validate_token(token)
        assert result is not None
        validated_user, claims = result
        assert validated_user.id == user.id
        assert claims["email"] == "t@t.com"
        assert claims["role"] == "viewer"

    def test_validate_expired_token(self, svc: AuthService) -> None:
        svc.config.jwt_expiry_seconds = -1
        user = svc._upsert_user("oidc", "sub-1", "t@t.com", "T", [])
        token = svc._issue_token(user)
        assert svc.validate_token(token) is None

    def test_validate_revoked_session(self, svc: AuthService) -> None:
        user = svc._upsert_user("oidc", "sub-1", "t@t.com", "T", [])
        token = svc._issue_token(user)

        # Revoke the session
        claims = verify_jwt(token, svc.config.jwt_secret)
        assert claims is not None
        svc.store.revoke_session(claims["session_id"])

        assert svc.validate_token(token) is None

    def test_legacy_token_validation(self, svc: AuthService) -> None:
        svc.config.legacy_token = "my-legacy-token"
        assert svc.validate_legacy_token("my-legacy-token")
        assert not svc.validate_legacy_token("wrong-token")
        svc.config.legacy_token = ""
        assert not svc.validate_legacy_token("my-legacy-token")

    def test_device_flow(self, svc: AuthService) -> None:
        # Create device request
        req = svc.create_device_request()
        assert req.device_code
        assert req.user_code

        # Not yet authorized
        assert svc.poll_device_token(req.device_code) is None

        # Authorize
        user = svc._upsert_user("oidc", "sub-1", "t@t.com", "T", [])
        assert svc.authorize_device(req.user_code, user)

        # Now polling should return a token
        result = svc.poll_device_token(req.device_code)
        assert result is not None
        token, status = result
        assert status == "complete"

        # Validate the issued token
        validated = svc.validate_token(token)
        assert validated is not None

    def test_device_flow_expired(self, svc: AuthService) -> None:
        req = DeviceAuthRequest(expires_at=time.time() - 1)
        svc.store.save_device_request(req)
        assert svc.poll_device_token(req.device_code) is None

    def test_logout(self, svc: AuthService) -> None:
        user = svc._upsert_user("oidc", "sub-1", "t@t.com", "T", [])
        token = svc._issue_token(user)
        claims = verify_jwt(token, svc.config.jwt_secret)
        assert claims is not None

        assert svc.logout(claims["session_id"])
        assert svc.validate_token(token) is None

    def test_saml_sp_metadata(self, svc: AuthService) -> None:
        svc.config.saml.sp_entity_id = "test-bernstein"
        svc.config.saml.sp_acs_url = "http://localhost:8052/auth/saml/acs"
        metadata = svc.get_saml_sp_metadata()
        assert "test-bernstein" in metadata
        assert "http://localhost:8052/auth/saml/acs" in metadata
        assert "SPSSODescriptor" in metadata

    def test_saml_auth_redirect_url(self, svc: AuthService) -> None:
        svc.config.saml.idp_sso_url = "https://idp.example.com/saml/sso"
        svc.config.saml.sp_entity_id = "bernstein"
        svc.config.saml.sp_acs_url = "http://localhost:8052/auth/saml/acs"
        url = svc.get_saml_auth_redirect_url(relay_state="test-relay")
        assert url.startswith("https://idp.example.com/saml/sso?")
        assert "SAMLRequest=" in url
        assert "RelayState=test-relay" in url


# ---------------------------------------------------------------------------
# Auth middleware tests
# ---------------------------------------------------------------------------


class TestSSOAuthMiddleware:
    """Tests for the SSO auth middleware logic (unit-level, no HTTP server)."""

    def test_public_paths_defined(self) -> None:
        from bernstein.core.auth_middleware import AUTH_PUBLIC_PATHS

        assert "/health" in AUTH_PUBLIC_PATHS
        assert "/auth/login" in AUTH_PUBLIC_PATHS
        assert "/auth/oidc/callback" in AUTH_PUBLIC_PATHS
        assert "/auth/saml/acs" in AUTH_PUBLIC_PATHS
        assert "/auth/cli/device" in AUTH_PUBLIC_PATHS
        assert "/webhook" in AUTH_PUBLIC_PATHS
        assert "/ready" in AUTH_PUBLIC_PATHS
        assert "/alive" in AUTH_PUBLIC_PATHS

    def test_route_permission_mapping(self) -> None:
        from bernstein.core.auth_middleware import _get_required_permission

        # Write operations
        assert _get_required_permission("/tasks", "POST") == "tasks:write"
        assert _get_required_permission("/agents/123/kill", "POST") == "agents:kill"

        # Read operations
        assert _get_required_permission("/tasks", "GET") == "tasks:read"
        assert _get_required_permission("/status", "GET") == "status:read"
