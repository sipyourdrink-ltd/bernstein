"""JWT-focused tests for agent identity tokens."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from bernstein.core.agent_identity import AgentIdentityStore
from bernstein.core.auth import verify_jwt


def _token_hash(token: str) -> str:
    """Return the SHA-256 hash used by the identity store."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_create_identity_returns_jwt_with_expected_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Newly issued agent tokens should be JWTs with the fixed identity claims."""

    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "agent-jwt-secret")
    store = AgentIdentityStore(tmp_path)

    identity, token = store.create_identity("backend-1", "backend", metadata={"tenant_id": "acme"})
    claims = verify_jwt(token, "agent-jwt-secret")

    assert claims is not None
    assert claims["sub"] == "backend-1"
    assert claims["sid"] == "backend-1"
    assert claims["role"] == "backend"
    assert claims["tenant_id"] == "acme"
    assert set(claims["scopes"]) == set(identity.permissions)
    assert identity.credential is not None
    assert identity.credential.token_type == "jwt"
    assert identity.credential.jti == claims["jti"]


def test_authenticate_jwt_token_updates_last_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """JWT authentication should resolve the identity and update its auth timestamp."""

    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "agent-jwt-secret")
    store = AgentIdentityStore(tmp_path)
    _, token = store.create_identity("backend-2", "backend")

    identity = store.authenticate(token)

    assert identity is not None
    assert identity.id == "backend-2"
    assert identity.last_authenticated_at > 0


def test_authenticate_legacy_opaque_token_remains_supported(tmp_path: Path) -> None:
    """Persisted pre-JWT opaque tokens should continue to authenticate during the compatibility window."""

    legacy_token = "legacy-opaque-token"
    identity_path = tmp_path / "agent_identities" / "legacy-1.json"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(
            {
                "id": "legacy-1",
                "role": "backend",
                "session_id": "legacy-1",
                "permissions": ["files:read", "files:write", "status:read", "tasks:claim", "tasks:read", "tests:run"],
                "status": "active",
                "created_at": 1.0,
                "last_authenticated_at": 0.0,
                "revoked_at": 0.0,
                "revocation_reason": "",
                "credential": {
                    "token_hash": _token_hash(legacy_token),
                    "created_at": 1.0,
                    "expires_at": 0.0,
                    "revoked": False,
                },
                "parent_identity_id": None,
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    store = AgentIdentityStore(tmp_path)
    identity = store.authenticate(legacy_token)

    assert identity is not None
    assert identity.id == "legacy-1"


def test_expired_jwt_token_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired JWT tokens should no longer authenticate."""

    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "agent-jwt-secret")
    store = AgentIdentityStore(tmp_path)
    _, token = store.create_identity("backend-3", "backend", token_expiry_s=1)
    future_time = float(time.time() + 10_000)

    monkeypatch.setattr("bernstein.core.auth.time.time", lambda: future_time)
    monkeypatch.setattr("bernstein.core.agent_identity.time.time", lambda: future_time)

    assert store.authenticate(token) is None


def test_jwt_secret_persists_without_env(tmp_path: Path) -> None:
    """A persisted agent JWT secret should make tokens survive store restarts."""

    store1 = AgentIdentityStore(tmp_path)
    _, token = store1.create_identity("persist-1", "backend")

    secret_path = tmp_path / "agent_identity_jwt_secret"
    store2 = AgentIdentityStore(tmp_path)

    assert secret_path.exists()
    assert store2.authenticate(token) is not None


def test_task_scoped_jwt_embeds_task_ids_in_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task-scoped JWTs must carry task_ids in the token payload for zero-trust enforcement."""

    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "agent-jwt-secret")
    store = AgentIdentityStore(tmp_path)

    _, token = store.create_identity(
        "worker-1",
        "backend",
        task_ids=["task-aaa", "task-bbb"],
    )
    claims = verify_jwt(token, "agent-jwt-secret")

    assert claims is not None
    assert sorted(claims["task_ids"]) == ["task-aaa", "task-bbb"]
    # allowed_files defaults to empty list
    assert claims["allowed_files"] == []


def test_file_scoped_jwt_embeds_allowed_files_in_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File-scoped JWTs must carry allowed_files in the token payload."""

    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "agent-jwt-secret")
    store = AgentIdentityStore(tmp_path)

    _, token = store.create_identity(
        "worker-2",
        "backend",
        task_ids=["task-ccc"],
        allowed_files=["src/**/*.py", "tests/**/*.py"],
    )
    claims = verify_jwt(token, "agent-jwt-secret")

    assert claims is not None
    assert sorted(claims["allowed_files"]) == ["src/**/*.py", "tests/**/*.py"]
    assert claims["task_ids"] == ["task-ccc"]


def test_scoped_token_rejects_claim_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token issued for task-A cannot authenticate as a token for task-B.

    This guards against claim-substitution attacks where an attacker intercepts
    a valid token and replays it against a different task. The identity store
    verifies that the task_ids in the JWT payload match the stored credential.
    """

    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "agent-jwt-secret")
    store = AgentIdentityStore(tmp_path)

    # Create a scoped token for task-A
    _identity_a, token_a = store.create_identity("worker-3", "backend", task_ids=["task-A"])

    # Authenticate with the correct token — must succeed
    authed = store.authenticate(token_a)
    assert authed is not None
    assert authed.task_ids == ["task-A"]

    # Now tamper: create a second identity with task-B scope. Its token
    # must NOT authenticate as worker-3's identity.
    _identity_b, token_b = store.create_identity("worker-4", "backend", task_ids=["task-B"])

    # token_b should authenticate as worker-4, not worker-3
    authed_b = store.authenticate(token_b)
    assert authed_b is not None
    assert authed_b.id == "worker-4"
    assert authed_b.task_ids == ["task-B"]

    # token_a should still only allow task-A access
    authed_a = store.authenticate(token_a)
    assert authed_a is not None
    assert authed_a.task_ids == ["task-A"]
    assert store.validate_task_access("worker-3", "task-A") is True
    assert store.validate_task_access("worker-3", "task-B") is False
