"""SCIM deprovisioning (``DELETE``/``PATCH`` on ``/Users/{id}``).

Holds three properties of the write surface:

* ADMIN, and only ADMIN, holds ``scim:write``, so an identity-provider
  credential for an admin can deprovision and any other role gets 403.
* Concurrent deprovisions append to the signed principal chain one at a time:
  the chain stays linear and verifies.
* Repeating a deprovision does not sign another record.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.identity import grants, principals
from bernstein.core.identity.agent_jwt import AgentIdentityStore
from bernstein.core.routes import scim
from bernstein.core.security.auth import (
    AuthRole,
    AuthService,
    AuthStore,
    AuthUser,
    SSOConfig,
    create_jwt,
    role_has_permission,
)
from bernstein.core.security.auth_middleware import SSOAuthMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_KEY = b"k" * 32
_SECRET = "scim-write-test-secret"  # NOSONAR - test fixture


def _build(
    tmp_path: Path, *, auth: bool
) -> tuple[FastAPI, AgentIdentityStore, principals.PrincipalLedger, dict[str, str]]:
    app = FastAPI()
    app.include_router(scim.router)
    store = AgentIdentityStore(tmp_path / "auth")
    ledger = principals.PrincipalLedger(
        root=tmp_path / "audit",
        key=_KEY,
        signer=grants.GrantSigner.generate(issuer="manager:test"),
    )
    app.state.identity_store = store
    app.state.principal_ledger = ledger
    app.state.runtime_dir = tmp_path / "runtime"
    tokens: dict[str, str] = {}
    if auth:
        auth_store = AuthStore(tmp_path)
        service = AuthService(SSOConfig(jwt_secret=_SECRET, enabled=True), auth_store)
        for role in AuthRole:
            user = AuthUser(id=f"u-{role.value}", email=f"{role.value}@example.com", display_name=role.value, role=role)
            auth_store.save_user(user)
            tokens[role.value] = create_jwt({"sub": user.id, "session_id": ""}, _SECRET, expiry_seconds=600)
        app.add_middleware(SSOAuthMiddleware, auth_service=service, legacy_token=None)
    return app, store, ledger, tokens


def _identity(store: AgentIdentityStore, name: str) -> str:
    identity, _token = store.create_identity(session_id=name, role="backend")
    return identity.id


def _records(ledger: principals.PrincipalLedger) -> list[str]:
    path = ledger.receipt_path()
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _chain(ledger: principals.PrincipalLedger) -> principals.PrincipalChainResult:
    return principals.verify_principal_chain(root=ledger.root, key=_KEY)


def _patch_body() -> dict[str, Any]:
    return {"Operations": [{"op": "replace", "path": "active", "value": False}]}


# ---------------------------------------------------------------------------
# Role table
# ---------------------------------------------------------------------------


def test_only_admin_holds_scim_write() -> None:
    assert role_has_permission(AuthRole.ADMIN, scim.SCIM_PERM_WRITE)
    for role in AuthRole:
        if role is not AuthRole.ADMIN:
            assert not role_has_permission(role, scim.SCIM_PERM_WRITE), role


@pytest.mark.auth_enabled
def test_admin_can_deprovision_and_other_roles_get_403(tmp_path: Path) -> None:
    app, store, ledger, tokens = _build(tmp_path, auth=True)
    victim = _identity(store, "victim")
    url = f"{scim.SCIM_BASE_PATH}/Users/{victim}"

    with TestClient(app) as client:
        for role in ("operator", "viewer"):
            headers = {"Authorization": f"Bearer {tokens[role]}"}
            assert client.delete(url, headers=headers).status_code == 403, role
            assert client.patch(url, json=_patch_body(), headers=headers).status_code == 403, role
        assert _records(ledger) == []
        assert store.get(victim).status.value == "active"

        admin = {"Authorization": f"Bearer {tokens['admin']}"}
        assert client.delete(url, headers=admin).status_code == 204

    assert len(_records(ledger)) == 1
    assert store.get(victim).status.value == "revoked"


@pytest.mark.auth_enabled
def test_admin_can_deprovision_with_patch(tmp_path: Path) -> None:
    app, store, ledger, tokens = _build(tmp_path, auth=True)
    victim = _identity(store, "victim")
    admin = {"Authorization": f"Bearer {tokens['admin']}"}

    with TestClient(app) as client:
        resp = client.patch(f"{scim.SCIM_BASE_PATH}/Users/{victim}", json=_patch_body(), headers=admin)

    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert len(_records(ledger)) == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _run_concurrently(count: int, call: Callable[[int], Any]) -> list[Any]:
    """Run ``call(i)`` on ``count`` threads released together; re-raise any failure.

    The barrier is bounded so a worker that fails before reaching it fails the
    test instead of leaving the others waiting.
    """
    start = threading.Barrier(count, timeout=30)

    def worker(i: int) -> Any:
        start.wait()
        return call(i)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(worker, i) for i in range(count)]
        return [f.result(timeout=60) for f in futures]


@pytest.mark.timeout(120)
def test_concurrent_deprovisions_keep_the_signed_chain_linear(tmp_path: Path) -> None:
    """Auth disabled: the handlers run in the threadpool with nothing else serialising them."""
    app, store, ledger, _ = _build(tmp_path, auth=False)
    ids = [_identity(store, f"agent-{i}") for i in range(8)]

    with TestClient(app) as client:

        def call(i: int) -> int:
            url = f"{scim.SCIM_BASE_PATH}/Users/{ids[i]}"
            resp = client.delete(url) if i % 2 else client.patch(url, json=_patch_body())
            return resp.status_code

        statuses = _run_concurrently(len(ids), call)

    assert sorted(set(statuses)) == [200, 204]
    result = _chain(ledger)
    assert result.valid, result.errors
    assert [r.record_index for r in result.records] == list(range(len(ids)))
    assert {r.principal_id for r in result.records} == set(ids)


@pytest.mark.timeout(120)
def test_ledger_appends_from_many_threads_do_not_fork(tmp_path: Path) -> None:
    ledger = principals.PrincipalLedger(
        root=tmp_path,
        key=_KEY,
        signer=grants.GrantSigner.generate(issuer="manager:test"),
    )
    count = 8

    _run_concurrently(count, lambda i: ledger.deprovision(principal_id=f"agent:{i}"))

    result = _chain(ledger)
    assert result.valid, result.errors
    assert [r.record_index for r in result.records] == list(range(count))


@pytest.mark.timeout(120)
def test_concurrent_repeats_of_one_deprovision_record_it_once(tmp_path: Path) -> None:
    app, store, ledger, _ = _build(tmp_path, auth=False)
    victim = _identity(store, "victim")
    count = 6

    with TestClient(app) as client:
        statuses = _run_concurrently(
            count, lambda _i: client.delete(f"{scim.SCIM_BASE_PATH}/Users/{victim}").status_code
        )

    assert statuses.count(204) == 1
    assert statuses.count(404) == count - 1
    assert len(_records(ledger)) == 1
    assert _chain(ledger).valid


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_repeat_deprovision_appends_no_record_and_keeps_the_first_reason(tmp_path: Path) -> None:
    app, store, ledger, _ = _build(tmp_path, auth=False)
    victim = _identity(store, "victim")
    url = f"{scim.SCIM_BASE_PATH}/Users/{victim}"

    with TestClient(app) as client:
        assert client.delete(url).status_code == 204
        first = _records(ledger)
        revoked_at = store.get(victim).revoked_at

        assert client.delete(url).status_code == 404
        assert client.patch(url, json=_patch_body()).status_code == 404

    assert _records(ledger) == first
    assert store.get(victim).revoked_at == revoked_at
    assert store.get(victim).revocation_reason == "scim_delete"


def test_retry_after_a_failed_revoke_does_not_sign_a_second_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, store, ledger, _ = _build(tmp_path, auth=False)
    victim = _identity(store, "victim")
    url = f"{scim.SCIM_BASE_PATH}/Users/{victim}"
    real_revoke = store.revoke
    calls = {"n": 0}

    def flaky_revoke(*args: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_revoke(*args, **kwargs)

    monkeypatch.setattr(store, "revoke", flaky_revoke)

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.delete(url).status_code == 500
        assert store.get(victim).status.value == "active"
        assert client.delete(url).status_code == 204

    assert len(_records(ledger)) == 1
    assert store.get(victim).status.value == "revoked"


def test_ledger_idempotent_deprovision_still_records_after_a_reprovision(tmp_path: Path) -> None:
    ledger = principals.PrincipalLedger(
        root=tmp_path,
        key=_KEY,
        signer=grants.GrantSigner.generate(issuer="manager:test"),
    )
    ledger.provision(principal_id="agent:a")
    first = ledger.deprovision(principal_id="agent:a", idempotent=True)
    again = ledger.deprovision(principal_id="agent:a", idempotent=True)
    assert again.record_index == first.record_index
    assert len(_records(ledger)) == 2

    ledger.provision(principal_id="agent:a")
    third = ledger.deprovision(principal_id="agent:a", idempotent=True)
    assert third.record_index == 3
    assert _chain(ledger).valid
