"""The task server honours only identities the orchestrator minted.

Runs the real application factory and the real auth middleware.  The caller
is a process with the reach a spawned agent has: it reads every file under
``.sdd/``, writes new records into ``.sdd/auth/agent_identities/``, and holds
a legitimately minted worker token of its own.  None of that may turn into a
request the server accepts with a role or permission the orchestrator did not
grant - here, cancelling a peer's task and killing a peer's agent.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from bernstein.core.identity.agent_jwt import AgentCredential, AgentIdentity, permissions_for_role
from bernstein.core.security.auth import create_jwt, decode_jwt_unverified

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-identity-binding-tests"
_VICTIM_SESSION = "backend-victim01"


def _make_app(tmp_path: Path) -> FastAPI:
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        plan_mode=True,
    )


def _client(application: FastAPI, index: int) -> TestClient:
    return TestClient(application, client=(f"10.41.{index // 256}.{index % 256}", 44000 + index))


def _create_task(application: FastAPI, index: int, title: str) -> str:
    response = _client(application, index).post(
        "/tasks",
        headers={"Authorization": f"Bearer {_OPERATOR_TOKEN}"},
        json={"title": title, "description": title, "role": "backend"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _forge(tmp_path: Path, identity_id: str, secret: str) -> dict[str, str]:
    """Write a manager record with ``agents:kill`` and sign a token for it."""
    permissions = permissions_for_role("manager") | {"agents:kill"}
    token = create_jwt(
        claims={
            "sub": identity_id,
            "sid": identity_id,
            "role": "manager",
            "scopes": sorted(permissions),
            "tenant_id": "default",
            "task_ids": [],
            "allowed_files": [],
        },
        secret=secret,
        expiry_seconds=3600,
    )
    claims = decode_jwt_unverified(token)
    assert claims is not None
    record = AgentIdentity(
        id=identity_id,
        role="manager",
        session_id=identity_id,
        permissions=permissions,
        credential=AgentCredential(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=float(claims["exp"]),
            token_type="jwt",
            jti=str(claims["jti"]),
        ),
    ).to_dict()
    path = tmp_path / ".sdd" / "auth" / "agent_identities" / f"{identity_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {"Authorization": f"Bearer {token}"}


def _readable_secrets(tmp_path: Path) -> list[str]:
    out: list[str] = []
    for path in sorted((tmp_path / ".sdd").rglob("*")):
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if text and "\n" not in text:
                out.append(text)
    return out


def _assert_refused(application: FastAPI, tmp_path: Path, headers: dict[str, str], task_id: str, index: int) -> None:
    cancel = _client(application, index).post(f"/tasks/{task_id}/cancel", headers=headers, json={"reason": "x"})
    kill = _client(application, index + 1).post(f"/agents/{_VICTIM_SESSION}/kill", headers=headers)

    assert cancel.status_code in {401, 403}, cancel.text
    assert kill.status_code in {401, 403}, kill.text
    assert not (tmp_path / ".sdd" / "runtime" / f"{_VICTIM_SESSION}.kill").exists()
    task = _client(application, index + 2).get(
        f"/tasks/{task_id}", headers={"Authorization": f"Bearer {_OPERATOR_TOKEN}"}
    )
    assert task.status_code == 200, task.text
    assert task.json()["status"] != "cancelled"


# ---------------------------------------------------------------------------
# (i) a forged manager identity
# ---------------------------------------------------------------------------


def test_forged_manager_signed_with_any_readable_file_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    victim_task = _create_task(app, 1, "peer-task")
    # A legitimately minted worker exists, as it would mid-run.
    app.state.identity_store.create_identity("worker-1", "backend", task_ids=[victim_task])

    candidates = _readable_secrets(tmp_path)
    assert candidates
    for index, secret in enumerate(candidates):
        headers = _forge(tmp_path, f"forged-{index}", secret)
        _assert_refused(app, tmp_path, headers, victim_task, 10 + 3 * index)


def test_forged_manager_is_refused_even_with_the_jwt_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding the token-signing secret does not make a self-written record count."""
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    app = _make_app(tmp_path)
    victim_task = _create_task(app, 2, "peer-task")

    headers = _forge(tmp_path, "forged-manager", "operator-shared-secret")

    _assert_refused(app, tmp_path, headers, victim_task, 20)


# ---------------------------------------------------------------------------
# (ii) a legitimate worker token carrying more than it was granted
# ---------------------------------------------------------------------------


def test_self_asserted_scopes_in_a_worker_token_do_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    app = _make_app(tmp_path)
    own_task = _create_task(app, 3, "own-task")
    victim_task = _create_task(app, 4, "peer-task")
    _, token = app.state.identity_store.create_identity("worker-1", "backend", task_ids=[own_task])
    claims = decode_jwt_unverified(token)
    assert claims is not None
    widened: dict[str, Any] = {k: v for k, v in claims.items() if k not in {"iat", "exp", "jti"}}
    widened["role"] = "manager"
    widened["scopes"] = sorted({*claims["scopes"], "agents:kill", "tasks:write"})
    widened["task_ids"] = []
    widened_token = create_jwt(widened, "operator-shared-secret", expiry_seconds=3600)

    _assert_refused(app, tmp_path, {"Authorization": f"Bearer {widened_token}"}, victim_task, 30)


def test_worker_rewriting_its_own_record_gains_nothing(tmp_path: Path) -> None:
    """The worker keeps its real token and widens the record behind it."""
    app = _make_app(tmp_path)
    own_task = _create_task(app, 5, "own-task")
    victim_task = _create_task(app, 6, "peer-task")
    _, token = app.state.identity_store.create_identity("worker-1", "backend", task_ids=[own_task])
    path = tmp_path / ".sdd" / "auth" / "agent_identities" / "worker-1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["permissions"] = sorted({*record["permissions"], "agents:kill", "tasks:write"})
    record["task_ids"] = []
    record["credential"]["task_ids"] = []
    path.write_text(json.dumps(record), encoding="utf-8")

    _assert_refused(app, tmp_path, {"Authorization": f"Bearer {token}"}, victim_task, 40)


# ---------------------------------------------------------------------------
# (iii) restart
# ---------------------------------------------------------------------------


def test_restart_does_not_accept_a_record_written_before_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    _make_app(tmp_path)
    headers = _forge(tmp_path, "forged-manager", "operator-shared-secret")

    restarted = _make_app(tmp_path)
    victim_task = _create_task(restarted, 7, "peer-task")

    _assert_refused(restarted, tmp_path, headers, victim_task, 50)


# ---------------------------------------------------------------------------
# (iv) what the orchestrator minted keeps working
# ---------------------------------------------------------------------------


def test_minted_worker_and_manager_tokens_still_authenticate(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    own_task = _create_task(app, 8, "own-task")
    other_task = _create_task(app, 9, "other-task")
    _, worker = app.state.identity_store.create_identity("worker-1", "backend", task_ids=[own_task])
    # Minted by a separate store object, as the orchestrator process does.
    from bernstein.core.identity.agent_jwt import AgentIdentityStore

    _, manager = AgentIdentityStore(tmp_path / ".sdd" / "auth").create_identity(
        "manager-1", "manager", extra_permissions=frozenset({"agents:kill"})
    )

    read = _client(app, 60).get(f"/tasks/{own_task}", headers={"Authorization": f"Bearer {worker}"})
    assert read.status_code == 200, read.text

    cancel = _client(app, 61).post(
        f"/tasks/{other_task}/cancel", headers={"Authorization": f"Bearer {manager}"}, json={"reason": "x"}
    )
    assert cancel.status_code == 200, cancel.text

    kill = _client(app, 62).post(f"/agents/{_VICTIM_SESSION}/kill", headers={"Authorization": f"Bearer {manager}"})
    assert kill.status_code == 200, kill.text

    restarted = _make_app(tmp_path)
    again = _client(restarted, 63).get("/tasks", headers={"Authorization": f"Bearer {worker}"})
    assert again.status_code == 200, again.text
