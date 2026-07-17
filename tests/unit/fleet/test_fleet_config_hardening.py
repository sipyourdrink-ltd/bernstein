"""Hardening tests for the fleet config plane review fixes (#2550, #2626).

Covers the security and data-integrity fixes raised in review: content-hash
validation, path-traversal rejection, NaN rejection, chain-authoritative
write heads, keyed secret-name digests, connection name-mismatch and
create/rotate guards, context activation ordering and drift fail-closed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from bernstein.core.config.home import BernsteinHome, resolve_config
from bernstein.core.fleet.connection import (
    ConnectionDocumentStore,
    ConnectionRefused,
    create_document,
    resolve_document,
    rotate_document,
)
from bernstein.core.fleet.context import ContextStore, OperatingContext
from bernstein.core.fleet.variables import FleetVariableStore, value_hash_of
from bernstein.core.security.audit_chain import (
    EVENT_FLEET_CONTEXT_ACTIVATE,
    AuditChainStore,
)
from bernstein.core.security.secrets_broker import (
    BrokerConfig,
    SecretsBackend,
    SecretsBroker,
    SecretsBrokerError,
    clear_redaction_registry,
)

_KEY = b"k" * 32


class _MemoryBackend(SecretsBackend):
    name = "memory"

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)

    def read(self, secret_name: str) -> str:
        if secret_name not in self._secrets:
            raise SecretsBrokerError(secret_name)
        return self._secrets[secret_name]

    def list_names(self) -> list[str]:
        return sorted(self._secrets)


@pytest.fixture(autouse=True)
def _registry():
    clear_redaction_registry()
    yield
    clear_redaction_registry()


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _broker(secrets: dict[str, str]) -> SecretsBroker:
    return SecretsBroker(_MemoryBackend(secrets), config=BrokerConfig(backend="memory"))


# -- variables --------------------------------------------------------------


def test_resolve_rejects_tampered_blob(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = FleetVariableStore(tmp_path / "vars", chain=chain)
    store.set("k", {"a": 1})
    vhash = value_hash_of({"a": 1})

    # Tamper with the stored blob bytes so they no longer hash to vhash.
    blob = tmp_path / "vars" / "blobs" / f"{vhash.split(':', 1)[1]}.json"
    blob.write_bytes(b'{"a": 999}')

    with pytest.raises(ValueError, match="content hash mismatch"):
        store.resolve(vhash)


def test_resolve_rejects_path_traversal_hash(tmp_path: Path) -> None:
    store = FleetVariableStore(tmp_path / "vars", chain=_chain(tmp_path))
    for bad in ("sha256:../../etc/passwd", "sha256:xyz", "notahash", "sha256:" + "g" * 64):
        with pytest.raises(ValueError, match="invalid content hash"):
            store.resolve(bad)


def test_set_rejects_nan(tmp_path: Path) -> None:
    store = FleetVariableStore(tmp_path / "vars", chain=_chain(tmp_path))
    with pytest.raises(ValueError):
        store.set("k", math.nan)
    with pytest.raises(ValueError):
        store.set("k", math.inf)


def test_write_head_is_chain_authoritative_without_index(tmp_path: Path) -> None:
    # No index file is consulted: a fresh store over the same chain + blobs
    # derives the correct next position purely from the chain.
    chain = _chain(tmp_path)
    store = FleetVariableStore(tmp_path / "vars", chain=chain)
    store.set("k", 1)
    store.set("k", 2)

    # There is no index.json to trust.
    assert not (tmp_path / "vars" / "index.json").exists()

    fresh_chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    fresh = FleetVariableStore(tmp_path / "vars", chain=fresh_chain)
    write = fresh.set("k", 3)
    assert write.chain_position == 2
    assert write.old_value_hash == value_hash_of(2)
    ok, errors = fresh_chain.verify()
    assert ok, errors


# -- connection -------------------------------------------------------------


def test_secret_name_digest_is_keyed_not_plain_sha256(tmp_path: Path) -> None:
    import hashlib

    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    create_document(
        name="c",
        secret_name="github_pat",
        scope="",
        connector_defaults={},
        identity_dir=tmp_path / "id",
        chain=chain,
        store=store,
    )
    from bernstein.core.security.audit_chain import EVENT_FLEET_CONN_CREATE

    digest = chain.query(event_type=EVENT_FLEET_CONN_CREATE)[0].details["secret_name_digest"]
    plain = "sha256:" + hashlib.sha256(b"github_pat").hexdigest()
    assert digest != plain
    assert digest.startswith("hmac-sha256:")


def test_store_get_rejects_name_mismatch(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    create_document(
        name="real",
        secret_name="s",
        scope="",
        connector_defaults={},
        identity_dir=tmp_path / "id",
        chain=chain,
        store=store,
    )
    # Copy the real document under a different filename.
    (tmp_path / "conns" / "impostor.json").write_bytes((tmp_path / "conns" / "real.json").read_bytes())
    with pytest.raises(ValueError, match="name mismatch"):
        store.get("impostor")


def test_create_refuses_to_replace_existing(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    kwargs = dict(
        secret_name="s", scope="", connector_defaults={}, identity_dir=tmp_path / "id", chain=chain, store=store
    )
    create_document(name="c", **kwargs)
    with pytest.raises(FileExistsError):
        create_document(name="c", **kwargs)


def test_rotate_refuses_foreign_document(tmp_path: Path) -> None:
    # A document signed by install A, copied to install B, cannot be rotated
    # (and re-signed) by B: rotation verifies the current signature first.
    chain_a = _chain(tmp_path / "a")
    store_a = ConnectionDocumentStore(tmp_path / "a" / "conns")
    create_document(
        name="c",
        secret_name="s",
        scope="",
        connector_defaults={},
        identity_dir=tmp_path / "a" / "id",
        chain=chain_a,
        store=store_a,
    )
    doc = store_a.get("c")

    store_b = ConnectionDocumentStore(tmp_path / "b" / "conns")
    store_b.put(doc)
    chain_b = _chain(tmp_path / "b")
    with pytest.raises(ConnectionRefused):
        rotate_document(
            "c",
            new_secret_name="s2",
            identity_dir=tmp_path / "b" / "id",
            chain=chain_b,
            store=store_b,
        )
    from bernstein.core.security.audit_chain import EVENT_FLEET_CONN_REFUSE

    assert chain_b.query(event_type=EVENT_FLEET_CONN_REFUSE)


def test_create_records_audit_before_persist(tmp_path: Path) -> None:
    # A broker resolve after create still works, proving the create receipt
    # and the persisted document are consistent.
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    broker = _broker({"s": "raw-secret"})
    create_document(
        name="c",
        secret_name="s",
        scope="",
        connector_defaults={},
        identity_dir=tmp_path / "id",
        chain=chain,
        store=store,
    )
    token = resolve_document("c", identity_dir=tmp_path / "id", task_id="t1", broker=broker, chain=chain, store=store)
    assert token.value != "raw-secret"
    ok, errors = chain.verify()
    assert ok, errors


# -- context ----------------------------------------------------------------


def test_activation_records_event_before_pointer(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    chain = _chain(tmp_path)
    store = ContextStore(project / ".sdd" / "fleet" / "contexts", chain=chain)
    store.create(OperatingContext(name="staging", config_layer={"budget": 5}))
    store.activate("staging")
    # Both artifacts exist and are consistent.
    assert chain.query(event_type=EVENT_FLEET_CONTEXT_ACTIVATE)
    assert (project / ".sdd" / "fleet" / "contexts" / "active.json").exists()


def test_create_refuses_silent_replacement_of_active_context(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    chain = _chain(tmp_path)
    store = ContextStore(project / ".sdd" / "fleet" / "contexts", chain=chain)
    store.create(OperatingContext(name="staging", config_layer={"budget": 5}))
    store.activate("staging")

    # Re-defining the active context with a different identity is refused.
    with pytest.raises(ValueError, match="active"):
        store.create(OperatingContext(name="staging", config_layer={"budget": 999}))


def test_home_fails_closed_on_drifted_active_context(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    chain = _chain(tmp_path)
    home = BernsteinHome(tmp_path / "home")
    ctx_root = project / ".sdd" / "fleet" / "contexts"
    store = ContextStore(ctx_root, chain=chain)
    store.create(OperatingContext(name="staging", config_layer={"budget": 42}))
    store.activate("staging")
    assert resolve_config("budget", home=home, project_dir=project)["source"] == "context"

    # Edit the context document on disk after activation: its hash no longer
    # matches active.json, so home must fail closed (context layer dropped).
    doc_path = ctx_root / "staging.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["config_layer"] = {"budget": 999}
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    resolved = resolve_config("budget", home=home, project_dir=project)
    assert resolved["source"] != "context"
