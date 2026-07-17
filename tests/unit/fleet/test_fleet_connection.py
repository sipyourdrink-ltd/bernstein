"""Tests for named connection documents (#2550).

A connection document is a typed, Ed25519-signed record that names a
broker-managed secret plus connector defaults; it carries no secret
material. Task specs reference it by name, so rotating one document
re-points every consumer at the next mint. Resolution runs only through
the broker mint path, emits a lineage receipt, and refuses (as a recorded
event) any document not signed by the local install identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.fleet.connection import (
    ConnectionDocumentStore,
    ConnectionRefused,
    create_document,
    resolve_document,
    rotate_document,
    verify_document_local,
)
from bernstein.core.security.audit_chain import (
    EVENT_FLEET_CONN_CREATE,
    EVENT_FLEET_CONN_REFUSE,
    EVENT_FLEET_CONN_RESOLVE,
    EVENT_FLEET_CONN_ROTATE,
    AuditChainStore,
)
from bernstein.core.security.secrets_broker import (
    BrokerConfig,
    SecretsBackend,
    SecretsBroker,
    SecretsBrokerError,
    clear_redaction_registry,
    get_redactable_values,
)

_KEY = b"k" * 32


class _MemoryBackend(SecretsBackend):
    name = "memory"

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)

    def read(self, secret_name: str) -> str:
        if secret_name not in self._secrets:
            raise SecretsBrokerError(f"no entry for {secret_name!r}")
        return self._secrets[secret_name]

    def list_names(self) -> list[str]:
        return sorted(self._secrets)


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_redaction_registry()
    yield
    clear_redaction_registry()


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _broker(secrets: dict[str, str]) -> SecretsBroker:
    return SecretsBroker(_MemoryBackend(secrets), config=BrokerConfig(backend="memory"))


def test_create_document_is_signed_and_recorded(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    identity = tmp_path / "identity"

    doc = create_document(
        name="prod-github",
        secret_name="github_pat",
        scope="repo:read",
        connector_defaults={"base_url": "https://api.github.com"},
        identity_dir=identity,
        chain=chain,
        store=store,
    )

    assert doc.name == "prod-github"
    assert doc.secret_name == "github_pat"
    assert doc.signature
    # The document carries no secret material.
    assert "github_pat" not in doc.signer_public_key_pem
    assert verify_document_local(doc, identity_dir=identity)

    events = chain.query(event_type=EVENT_FLEET_CONN_CREATE)
    assert len(events) == 1
    assert events[0].details["name"] == "prod-github"
    assert events[0].details["document_hash"] == doc.document_hash()
    ok, errs = chain.verify()
    assert ok, errs


def test_document_persisted_without_secret_value(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    identity = tmp_path / "identity"
    # Populate connector_defaults with real (non-secret) connector config so
    # the persistence assertion is not vacuous: the document carries operator
    # defaults, references a broker secret by name, and never a secret value.
    create_document(
        name="team-slack",
        secret_name="slack_token",
        scope="chat:write",
        connector_defaults={"base_url": "https://slack.com/api", "timeout_s": 30},
        identity_dir=identity,
        chain=chain,
        store=store,
    )
    on_disk = (tmp_path / "conns" / "team-slack.json").read_text(encoding="utf-8")
    assert "slack_token" in on_disk  # the *reference* name is fine
    assert "https://slack.com/api" in on_disk  # non-secret connector default persisted
    assert "xoxb" not in on_disk  # no secret value
    # The document schema has no field that carries a raw secret value; only a
    # broker secret *reference*. A secret is resolved solely through the broker
    # mint path, never persisted here.
    doc = store.get("team-slack")
    assert doc.secret_name == "slack_token"
    assert not hasattr(doc, "secret_value")


def test_resolve_goes_through_broker_and_emits_receipt(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    identity = tmp_path / "identity"
    broker = _broker({"slack_token": "xoxb-super-secret"})
    create_document(
        name="team-slack",
        secret_name="slack_token",
        scope="chat:write",
        connector_defaults={},
        identity_dir=identity,
        chain=chain,
        store=store,
    )

    token = resolve_document(
        "team-slack",
        identity_dir=identity,
        task_id="task-123",
        broker=broker,
        chain=chain,
        store=store,
    )

    # Raw backing secret never appears in the token; it is registered for
    # redaction so it can never reach an agent environment or artifact.
    assert token.value != "xoxb-super-secret"
    assert "xoxb-super-secret" in get_redactable_values()

    receipts = chain.query(event_type=EVENT_FLEET_CONN_RESOLVE)
    assert len(receipts) == 1
    assert receipts[0].details["task_id"] == "task-123"
    assert receipts[0].details["token_id"] == token.token_id
    assert receipts[0].details["name"] == "team-slack"


def test_conn_audit_reconstructs_resolving_tasks_offline(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    identity = tmp_path / "identity"
    broker = _broker({"slack_token": "xoxb-secret"})
    create_document(
        name="team-slack",
        secret_name="slack_token",
        scope="chat:write",
        connector_defaults={},
        identity_dir=identity,
        chain=chain,
        store=store,
    )
    for task in ("t1", "t2", "t3"):
        resolve_document(
            "team-slack",
            identity_dir=identity,
            task_id=task,
            broker=broker,
            chain=chain,
            store=store,
        )

    # Reconstruct offline from a fresh chain reader (no server running).
    offline = AuditChainStore(tmp_path / "audit", key=_KEY)
    from bernstein.core.fleet.connection import audit_resolutions

    resolving = audit_resolutions(offline, name="team-slack")
    assert [r.task_id for r in resolving] == ["t1", "t2", "t3"]


def test_copied_document_refuses_and_records_refusal(tmp_path: Path) -> None:
    # Isolation: a document signed by install A, copied to install B, refuses
    # to resolve because signature verification against B's local identity
    # fails - and the refusal is itself an audit event.
    chain_a = _chain(tmp_path / "a")
    store_a = ConnectionDocumentStore(tmp_path / "a" / "conns")
    identity_a = tmp_path / "a" / "identity"
    broker = _broker({"slack_token": "secret"})
    create_document(
        name="team-slack",
        secret_name="slack_token",
        scope="chat:write",
        connector_defaults={},
        identity_dir=identity_a,
        chain=chain_a,
        store=store_a,
    )

    # Copy the document verbatim into install B, which has a *different*
    # local identity and its own chain.
    doc_a = store_a.get("team-slack")
    chain_b = _chain(tmp_path / "b")
    store_b = ConnectionDocumentStore(tmp_path / "b" / "conns")
    identity_b = tmp_path / "b" / "identity"
    store_b.put(doc_a)  # verbatim copy

    assert not verify_document_local(doc_a, identity_dir=identity_b)
    with pytest.raises(ConnectionRefused):
        resolve_document(
            "team-slack",
            identity_dir=identity_b,
            task_id="task-x",
            broker=broker,
            chain=chain_b,
            store=store_b,
        )

    refusals = AuditChainStore(tmp_path / "b" / "audit", key=_KEY).query(event_type=EVENT_FLEET_CONN_REFUSE)
    assert len(refusals) == 1
    assert refusals[0].details["reason"] == "signature_verification_failed"


def test_rotation_is_signed_event_and_repoints_consumers(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    identity = tmp_path / "identity"
    broker = _broker({"slack_token_v1": "old", "slack_token_v2": "new-secret"})
    create_document(
        name="team-slack",
        secret_name="slack_token_v1",
        scope="chat:write",
        connector_defaults={},
        identity_dir=identity,
        chain=chain,
        store=store,
    )
    old_doc = store.get("team-slack")

    rotated = rotate_document(
        "team-slack",
        new_secret_name="slack_token_v2",
        identity_dir=identity,
        chain=chain,
        store=store,
    )
    assert rotated.secret_name == "slack_token_v2"
    assert rotated.version == old_doc.version + 1
    assert verify_document_local(rotated, identity_dir=identity)

    rot_events = chain.query(event_type=EVENT_FLEET_CONN_ROTATE)
    assert len(rot_events) == 1
    assert rot_events[0].details["old_document_hash"] == old_doc.document_hash()
    assert rot_events[0].details["new_document_hash"] == rotated.document_hash()

    # A consumer that references the document by name (zero spec edits) now
    # resolves the rotated secret at the next mint.
    token = resolve_document(
        "team-slack",
        identity_dir=identity,
        task_id="task-after-rotate",
        broker=broker,
        chain=chain,
        store=store,
    )
    assert broker.resolve(token.value) == "new-secret"
    assert "new-secret" in get_redactable_values()
