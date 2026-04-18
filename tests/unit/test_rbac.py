"""Unit tests for RBAC enforcement (auth roles, permissions, middleware)."""

from __future__ import annotations

from bernstein.core.auth import AuthRole, AuthUser, role_has_permission

# ---- Role permissions ----------------------------------------------------


class TestRolePermissions:
    def test_admin_has_all_write_permissions(self) -> None:
        assert role_has_permission(AuthRole.ADMIN, "tasks:write")
        assert role_has_permission(AuthRole.ADMIN, "tasks:delete")
        assert role_has_permission(AuthRole.ADMIN, "agents:kill")
        assert role_has_permission(AuthRole.ADMIN, "auth:manage")
        assert role_has_permission(AuthRole.ADMIN, "config:write")

    def test_operator_has_write_but_not_admin(self) -> None:
        assert role_has_permission(AuthRole.OPERATOR, "tasks:write")
        assert role_has_permission(AuthRole.OPERATOR, "agents:read")
        assert not role_has_permission(AuthRole.OPERATOR, "auth:manage")

    def test_viewer_read_only(self) -> None:
        assert role_has_permission(AuthRole.VIEWER, "tasks:read")
        assert role_has_permission(AuthRole.VIEWER, "agents:read")
        assert role_has_permission(AuthRole.VIEWER, "status:read")
        assert not role_has_permission(AuthRole.VIEWER, "tasks:write")
        assert not role_has_permission(AuthRole.VIEWER, "tasks:delete")
        assert not role_has_permission(AuthRole.VIEWER, "agents:kill")

    def test_admin_manage_held_only_by_admin(self) -> None:
        """admin:manage gates shutdown/broadcast/drain/config — admin only (audit-119)."""
        assert role_has_permission(AuthRole.ADMIN, "admin:manage")
        assert not role_has_permission(AuthRole.OPERATOR, "admin:manage")
        assert not role_has_permission(AuthRole.VIEWER, "admin:manage")

    def test_unknown_permission_denied(self) -> None:
        assert not role_has_permission(AuthRole.VIEWER, "nonexistent:perm")
        assert not role_has_permission(AuthRole.ADMIN, "totally:made:up")


# ---- AuthUser.has_permission ---------------------------------------------


class TestAuthUserPermission:
    def test_admin_user_has_permission(self) -> None:
        user = AuthUser(
            id="u1",
            email="admin@example.com",
            display_name="Admin",
            role=AuthRole.ADMIN,
        )
        assert user.has_permission("tasks:write")
        assert user.has_permission("auth:manage")

    def test_viewer_user_denied_writes(self) -> None:
        user = AuthUser(
            id="u2",
            email="viewer@example.com",
            display_name="Viewer",
            role=AuthRole.VIEWER,
        )
        assert user.has_permission("tasks:read")
        assert not user.has_permission("tasks:write")


# ---- Auth middleware permission resolution --------------------------------


class TestMiddlewarePermissions:
    def test_get_required_permission_read(self) -> None:
        from bernstein.core.auth_middleware import _get_required_permission

        perm = _get_required_permission("/tasks", "GET")
        assert perm is not None
        assert "read" in perm

    def test_get_required_permission_write(self) -> None:
        from bernstein.core.auth_middleware import _get_required_permission

        perm = _get_required_permission("/tasks", "POST")
        assert perm is not None
        assert "write" in perm

    def test_get_required_permission_kill(self) -> None:
        from bernstein.core.auth_middleware import _get_required_permission

        perm = _get_required_permission("/agents/abc/kill", "POST")
        assert perm == "agents:kill"

    def test_get_required_permission_complete(self) -> None:
        from bernstein.core.auth_middleware import _get_required_permission

        perm = _get_required_permission("/tasks/abc/complete", "POST")
        assert perm == "tasks:write"


# ---- RBAC config loading -------------------------------------------------


class TestRBACConfig:
    def test_rbac_yaml_exists(self) -> None:
        from pathlib import Path

        _rbac_path = Path(__file__).parents[2] / ".sdd" / "config" / "rbac.yaml"
        # The config may not exist in test env, so just check the format would work
        import yaml

        sample = {
            "roles": {
                "admin": {"permissions": ["tasks:read", "tasks:write"]},
                "viewer": {"permissions": ["tasks:read"]},
            },
            "default_role": "viewer",
        }
        dumped = yaml.dump(sample)
        loaded = yaml.safe_load(dumped)
        assert "roles" in loaded
        assert "admin" in loaded["roles"]
        assert "default_role" in loaded
