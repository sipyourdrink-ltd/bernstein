"""Identity records are authenticated by a key that does not live under ``.sdd/``.

A spawned agent runs as the same OS user inside ``.sdd/worktrees/<session>/``,
so it can read every file under ``.sdd/`` and write new ones there.  The store
therefore cannot treat "a well-formed record exists on disk" as evidence that
the orchestrator minted it: role, permissions and task scope are only honoured
when the record carries a MAC under a key derived from the install key, which
lives outside the workdir.

Each test below takes the position of a process with exactly that reach - it
reads whatever ``.sdd/`` holds and writes records next to the real ones - and
pins that it cannot come away with an identity the store accepts.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.identity.agent_jwt import (
    AgentCredential,
    AgentIdentity,
    AgentIdentityStore,
    permissions_for_role,
)
from bernstein.core.security.auth import create_jwt, decode_jwt_unverified

if TYPE_CHECKING:
    from pathlib import Path


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sdd_file_contents(root: Path) -> list[str]:
    """Every non-empty text file under *root*: what an agent can read."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            found.append(text)
    return found


def _write_forged_record(
    auth_dir: Path,
    identity_id: str,
    secret: str,
    *,
    role: str = "manager",
    extra: frozenset[str] = frozenset({"agents:kill"}),
    mac_from: Path | None = None,
) -> str:
    """Write a self-made identity record and return a token signed for it."""
    permissions = permissions_for_role(role) | extra
    token = create_jwt(
        claims={
            "sub": identity_id,
            "sid": identity_id,
            "role": role,
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
    identity = AgentIdentity(
        id=identity_id,
        role=role,
        session_id=identity_id,
        permissions=permissions,
        credential=AgentCredential(
            token_hash=_token_hash(token),
            expires_at=float(claims["exp"]),
            token_type="jwt",
            jti=str(claims["jti"]),
        ),
    )
    record: dict[str, Any] = identity.to_dict()
    if mac_from is not None:
        # Borrow the authentication field of a genuine record verbatim.
        genuine = json.loads(mac_from.read_text(encoding="utf-8"))
        for key, value in genuine.items():
            if key not in record:
                record[key] = value
    path = auth_dir / "agent_identities" / f"{identity_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return token


# ---------------------------------------------------------------------------
# Key material is not under .sdd/
# ---------------------------------------------------------------------------


def test_no_signing_secret_is_written_under_the_auth_dir(tmp_path: Path) -> None:
    store = AgentIdentityStore(tmp_path)
    _, token = store.create_identity("worker-1", "backend", task_ids=["t1"])

    assert not (tmp_path / "agent_identity_jwt_secret").exists()
    # No file the agent can read verifies the token it was handed.
    from bernstein.core.security.auth import verify_jwt

    for candidate in _sdd_file_contents(tmp_path):
        assert verify_jwt(token, candidate) is None


def test_forged_record_signed_with_every_readable_file_is_refused(tmp_path: Path) -> None:
    """Whatever the agent can read under ``.sdd/`` does not let it mint."""
    store = AgentIdentityStore(tmp_path)
    store.create_identity("worker-1", "backend", task_ids=["t1"])

    candidates = _sdd_file_contents(tmp_path)
    assert candidates
    for index, secret in enumerate(candidates):
        token = _write_forged_record(tmp_path, f"forged-{index}", secret)
        assert store.authenticate(token) is None


# ---------------------------------------------------------------------------
# Knowing the JWT secret is not enough either
# ---------------------------------------------------------------------------


def test_forged_record_with_known_jwt_secret_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record, not the token signature, is the authority for a role.

    The operator-supplied secret is in the environment here, so the forger
    holds it: the signature on the token is valid.  The record it wrote is not
    one the store minted, so the token does not authenticate.
    """
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    store = AgentIdentityStore(tmp_path)

    token = _write_forged_record(tmp_path, "forged-manager", "operator-shared-secret")

    assert store.authenticate(token) is None
    assert store.get("forged-manager") is None
    assert store.authorize("forged-manager", "agents:kill") is False
    assert all(i.id != "forged-manager" for i in store.list_identities())


def test_forged_record_borrowing_a_genuine_mac_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    store = AgentIdentityStore(tmp_path)
    store.create_identity("worker-1", "backend", task_ids=["t1"])
    genuine = tmp_path / "agent_identities" / "worker-1.json"

    token = _write_forged_record(tmp_path, "forged-manager", "operator-shared-secret", mac_from=genuine)

    assert store.authenticate(token) is None


def test_editing_own_record_to_widen_permissions_is_refused(tmp_path: Path) -> None:
    """A worker that rewrites its own record loses the record, not gains a grant.

    Its token still hashes to the stored value, so this also pins that the
    opaque-token fallback does not authenticate a JWT credential whose claim
    check failed.
    """
    store = AgentIdentityStore(tmp_path)
    _, token = store.create_identity("worker-1", "backend", task_ids=["t1"])
    path = tmp_path / "agent_identities" / "worker-1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["role"] = "manager"
    record["permissions"] = sorted({*record["permissions"], "agents:kill", "tasks:write"})
    record["task_ids"] = []
    record["credential"]["task_ids"] = []
    path.write_text(json.dumps(record), encoding="utf-8")

    assert store.authenticate(token) is None
    assert store.authorize("worker-1", "agents:kill") is False


def test_token_with_self_asserted_scopes_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extra scopes in a re-signed token grant nothing."""
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    store = AgentIdentityStore(tmp_path)
    identity, token = store.create_identity("worker-1", "backend", task_ids=["t1"])
    claims = decode_jwt_unverified(token)
    assert claims is not None
    widened = {k: v for k, v in claims.items() if k not in {"iat", "exp", "jti"}}
    widened["scopes"] = sorted({*claims["scopes"], "agents:kill"})
    widened_token = create_jwt(widened, "operator-shared-secret", expiry_seconds=3600)

    assert store.authenticate(widened_token) is None
    authed = store.authenticate(token)
    assert authed is not None
    assert authed.permissions == identity.permissions
    assert "agents:kill" not in authed.permissions


def test_record_copied_under_another_id_is_refused(tmp_path: Path) -> None:
    store = AgentIdentityStore(tmp_path)
    store.create_identity("worker-1", "backend", task_ids=["t1"])
    identities = tmp_path / "agent_identities"
    (identities / "worker-2.json").write_text((identities / "worker-1.json").read_text(encoding="utf-8"))

    assert store.get("worker-2") is None


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def test_restart_does_not_resurrect_an_unauthenticated_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-shared-secret")
    AgentIdentityStore(tmp_path)
    token = _write_forged_record(tmp_path, "forged-manager", "operator-shared-secret")

    with caplog.at_level("WARNING"):
        restarted = AgentIdentityStore(tmp_path)

    assert restarted.authenticate(token) is None
    assert restarted.get("forged-manager") is None
    assert "forged-manager" not in restarted._token_index.values()
    assert any("forged-manager" in record.getMessage() for record in caplog.records)


def test_restart_keeps_minted_tokens_valid(tmp_path: Path) -> None:
    """Records the store minted stay valid for a fresh store over the same dir."""
    first = AgentIdentityStore(tmp_path)
    _, worker_token = first.create_identity("worker-1", "backend", task_ids=["t1"])
    _, manager_token = first.create_identity("run-root-r1", "manager")

    restarted = AgentIdentityStore(tmp_path)

    worker = restarted.authenticate(worker_token)
    manager = restarted.authenticate(manager_token)
    assert worker is not None
    assert worker.task_ids == ["t1"]
    assert manager is not None
    assert manager.role == "manager"


def test_revocation_and_suspension_survive_reload(tmp_path: Path) -> None:
    """Lifecycle writes are re-authenticated, so they remain readable."""
    store = AgentIdentityStore(tmp_path)
    _, token = store.create_identity("worker-1", "backend", task_ids=["t1"])
    assert store.suspend("worker-1")
    assert AgentIdentityStore(tmp_path).authenticate(token) is None
    assert store.reactivate("worker-1")
    assert AgentIdentityStore(tmp_path).authenticate(token) is not None
    assert store.revoke("worker-1")

    reloaded = AgentIdentityStore(tmp_path).get("worker-1")
    assert reloaded is not None
    assert reloaded.status.value == "revoked"


def test_legacy_secret_file_is_not_used(tmp_path: Path) -> None:
    """A secret left in the auth dir by an older version signs nothing."""
    legacy = tmp_path / "agent_identity_jwt_secret"
    legacy.write_text("legacy-in-tree-secret", encoding="utf-8")

    store = AgentIdentityStore(tmp_path)
    token = _write_forged_record(tmp_path, "forged-manager", "legacy-in-tree-secret")

    assert store.authenticate(token) is None
    assert not legacy.exists()
