"""Tests for named connection documents (#2550).

A connection document is a typed, Ed25519-signed record that names a
broker-managed secret plus connector defaults; it carries no secret
material. Task specs reference it by name, so rotating one document
re-points every consumer at the next mint. Resolution runs only through
the broker mint path, emits a lineage receipt, and refuses (as a recorded
event) any document not signed by the local install identity.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bernstein.core.fleet.connection import (
    ConnectionDocument,
    ConnectionDocumentStore,
    ConnectionReferenceError,
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
        broker_ref="github_pat",
        scope="repo:read",
        connector_defaults={"base_url": "https://api.github.com"},
        identity_dir=identity,
        chain=chain,
        store=store,
    )

    assert doc.name == "prod-github"
    assert doc.broker_ref == "github_pat"
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
        broker_ref="slack_token",
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
    assert doc.broker_ref == "slack_token"
    assert not hasattr(doc, "secret_value")


def test_resolve_goes_through_broker_and_emits_receipt(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = ConnectionDocumentStore(tmp_path / "conns")
    identity = tmp_path / "identity"
    broker = _broker({"slack_token": "xoxb-super-secret"})
    create_document(
        name="team-slack",
        broker_ref="slack_token",
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
        broker_ref="slack_token",
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
        broker_ref="slack_token",
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
        broker_ref="slack_token_v1",
        scope="chat:write",
        connector_defaults={},
        identity_dir=identity,
        chain=chain,
        store=store,
    )
    old_doc = store.get("team-slack")

    rotated = rotate_document(
        "team-slack",
        new_broker_ref="slack_token_v2",
        identity_dir=identity,
        chain=chain,
        store=store,
    )
    assert rotated.broker_ref == "slack_token_v2"
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


class TestNoSecretMaterialOnDisk:
    """The document is signed and persisted in the clear, so the invariant that
    it *names* a secret rather than carrying one has to be enforced, not just
    documented."""

    _CREDENTIAL_SHAPES = [
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN\n-----END PRIVATE KEY-----",
        '{"type": "service_account", "private_key": "abc"}',
        "token with spaces",
        "trailing-newline\n",
        "tab\tseparated",
        "x" * 257,
        "",
    ]

    @pytest.mark.parametrize("material", _CREDENTIAL_SHAPES)
    def test_put_refuses_a_credential_shaped_reference(self, tmp_path: Path, material: str) -> None:
        """The write sink is where the shape is enforced, so nothing unshaped
        can reach disk by any route that persists a document."""
        store = ConnectionDocumentStore(tmp_path / "conns")
        doc = ConnectionDocument(name="c", broker_ref=material, scope="")
        with pytest.raises(ConnectionReferenceError, match="broker reference"):
            store.put(doc)
        assert store.list_names() == []

    def test_construction_does_not_enforce_the_shape(self) -> None:
        """Construction must stay permissive so a document written by an
        earlier release can still be parsed. Enforcement lives on the write
        path; see the back-compat class below."""
        doc = ConnectionDocument(name="c", broker_ref="my secret name", scope="")
        assert doc.broker_ref == "my secret name"

    def test_refusal_is_narrow_enough_to_distinguish_from_other_failures(self, tmp_path: Path) -> None:
        """The refusal is its own type so a caller can report it as operator
        input error without also swallowing an unrelated failure."""
        assert issubclass(ConnectionReferenceError, ValueError)
        store = ConnectionDocumentStore(tmp_path / "conns")
        with pytest.raises(ConnectionReferenceError):
            store.put(ConnectionDocument(name="c", broker_ref="has spaces", scope=""))
        # A malformed *name* is a different failure and keeps the plain type.
        with pytest.raises(ValueError) as seen:
            store._path("../escape")
        assert not isinstance(seen.value, ConnectionReferenceError)

    @pytest.mark.parametrize("material", _CREDENTIAL_SHAPES)
    def test_credential_shaped_reference_never_reaches_disk(self, tmp_path: Path, material: str) -> None:
        chain = _chain(tmp_path)
        store = ConnectionDocumentStore(tmp_path / "conns")
        with pytest.raises(ValueError, match="broker reference"):
            create_document(
                name="prod-github",
                broker_ref=material,
                scope="repo:read",
                connector_defaults={},
                identity_dir=tmp_path / "identity",
                chain=chain,
                store=store,
            )
        assert store.list_names() == []
        written = [p for p in (tmp_path / "conns").rglob("*") if p.is_file()] if (tmp_path / "conns").exists() else []
        assert written == []

    def test_persisted_bytes_hold_the_reference_never_the_value(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        store = ConnectionDocumentStore(tmp_path / "conns")
        secret_value = "ghp_this_is_the_actual_token_value"
        create_document(
            name="prod-github",
            broker_ref="GITHUB_TOKEN",
            scope="repo:read",
            connector_defaults={"base_url": "https://api.github.com"},
            identity_dir=tmp_path / "identity",
            chain=chain,
            store=store,
        )
        on_disk = (tmp_path / "conns" / "prod-github.json").read_text(encoding="utf-8")
        assert "GITHUB_TOKEN" in on_disk
        assert secret_value not in on_disk

        # Nothing anywhere under the store or the chain carries the value.
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert secret_value not in path.read_bytes().decode("utf-8", "replace")

    def test_wire_key_is_stable_so_signed_documents_still_verify(self, tmp_path: Path) -> None:
        """The wire key is inside the signed preimage and the document hash.

        Renaming the attribute must not move the key, or every document ever
        signed would fail to verify and every recorded hash would dangle.
        """
        import json as _json

        doc = ConnectionDocument(
            name="prod-github",
            broker_ref="GITHUB_TOKEN",
            scope="repo:read",
            connector_defaults={"base_url": "https://api.github.com"},
        )
        payload = _json.loads(doc.to_json())
        assert payload["secret_name"] == "GITHUB_TOKEN"
        assert "broker_ref" not in payload
        assert doc.document_hash() == "sha256:4a34c5e7682f18ada746b01aa6595edd190c1e59b53704eae7c8e6e4d7e341a6"
        assert ConnectionDocument.from_json(doc.to_json()) == doc


class TestLegacyDocumentCompatibility:
    """A document written before the reference shape was enforced must stay
    usable and remediable.

    Enforcing the shape on the read path would strand an operator mid-upgrade:
    refusing to parse a document already on disk cannot un-write it, and
    rotation - the only remedy - has to load the old document first.
    """

    @staticmethod
    def _write_legacy(tmp_path: Path, broker_ref: str = "my secret name") -> ConnectionDocumentStore:
        """Write a document exactly as an earlier release did.

        Signed by the local install identity, but with a reference the current
        shape check rejects, and written straight to disk rather than through
        ``put`` (which is where the check now lives).
        """
        from bernstein.core.fleet.connection import _local_identity, _sign

        root = tmp_path / ".sdd" / "fleet" / "connections"
        root.mkdir(parents=True)
        private_key_pem, public_key_pem = _local_identity(tmp_path / ".sdd" / "identity")
        doc = _sign(
            ConnectionDocument(name="legacy", broker_ref=broker_ref, scope="", signer_public_key_pem=public_key_pem),
            private_key_pem,
        )
        (root / "legacy.json").write_text(doc.to_json(), encoding="utf-8")
        return ConnectionDocumentStore(root)

    def test_legacy_document_still_loads(self, tmp_path: Path) -> None:
        store = self._write_legacy(tmp_path)
        assert store.get("legacy").broker_ref == "my secret name"

    def test_one_legacy_document_does_not_break_the_whole_listing(self, tmp_path: Path) -> None:
        """`conn list` iterates get() with no handler, so a refusal on load
        would take out the entire store, not just the one document."""
        store = self._write_legacy(tmp_path)
        assert [store.get(n).name for n in store.list_names()] == ["legacy"]

    def test_load_warns_with_the_remediating_command(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        store = self._write_legacy(tmp_path)
        with caplog.at_level(logging.WARNING, logger="bernstein.core.fleet.connection"):
            store.get("legacy")
        assert caplog.records, "a legacy reference must be reported, not silently accepted"
        assert "conn rotate" in caplog.records[0].getMessage()

    def test_warning_never_reveals_the_reference_value(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """An unshaped reference may BE a pasted credential, so the warning
        describes its shape and must never reproduce it."""
        secret = "ghp_actual_token_value_pasted_by_mistake with spaces"
        store = self._write_legacy(tmp_path, broker_ref=secret)
        with caplog.at_level(logging.WARNING, logger="bernstein.core.fleet.connection"):
            store.get("legacy")
        rendered = caplog.records[0].getMessage()
        assert secret not in rendered
        assert "ghp_actual_token_value" not in rendered
        assert str(len(secret)) in rendered

    def test_rotation_remediates_a_legacy_document(self, tmp_path: Path) -> None:
        store = self._write_legacy(tmp_path)
        rotated = rotate_document(
            "legacy",
            new_broker_ref="GOOD_NAME",
            identity_dir=tmp_path / ".sdd" / "identity",
            chain=_chain(tmp_path),
            store=store,
        )
        assert rotated.broker_ref == "GOOD_NAME"
        assert store.get("legacy").broker_ref == "GOOD_NAME"

    def test_rotating_without_a_new_reference_names_the_remedy(self, tmp_path: Path) -> None:
        """Carrying the legacy reference forward would re-persist it. The
        refusal must point at the missing --secret, not blame an argument the
        operator never passed."""
        store = self._write_legacy(tmp_path)
        with pytest.raises(ConnectionReferenceError) as seen:
            rotate_document(
                "legacy",
                new_scope="repo:write",
                identity_dir=tmp_path / ".sdd" / "identity",
                chain=_chain(tmp_path),
                store=store,
            )
        assert "--secret" in str(seen.value)
        assert "my secret name" not in str(seen.value)

    def test_a_refused_create_leaves_no_chain_receipt(self, tmp_path: Path) -> None:
        """The create receipt is recorded before the document is written, so
        the reference must be refused before the chain is touched or the chain
        would carry a receipt for a document that never reached disk."""
        from bernstein.core.security.audit_chain import EVENT_FLEET_CONN_CREATE

        chain = _chain(tmp_path)
        store = ConnectionDocumentStore(tmp_path / "conns")
        with pytest.raises(ConnectionReferenceError):
            create_document(
                name="pasted",
                broker_ref="-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----",
                scope="",
                connector_defaults={},
                identity_dir=tmp_path / "identity",
                chain=chain,
                store=store,
            )
        assert chain.query(event_type=EVENT_FLEET_CONN_CREATE) == []
        assert store.list_names() == []


class TestHashCoversWhatIsPersisted:
    """The document hash must be computed over what actually lands on disk.

    A self-consistency check that recomputes a hash from persisted data breaks
    on honest records if anything lossy sits between the in-memory object and
    the stored bytes: a redaction applied on write but not before hashing, a
    falsy value coerced on read, or a second serialiser with different
    canonicalisation. The hash, the signed preimage, and the file all derive
    from one `_payload`, and these tests hold that.
    """

    _SHAPES = {
        "plain": ("repo:read", {"base_url": "https://api.github.com"}),
        # Falsy values are the direct analogue of an `or`-coercion bug: they
        # must survive as themselves, not be replaced by a default on read.
        "falsy_empty": ("", {}),
        "falsy_nested": ("", {"a": 0, "b": "", "c": False, "d": None, "e": [], "f": {}}),
        "non_ascii": ("portée", {"note": "naïve café"}),
        "numeric": ("s", {"i": 1, "f": 1.0, "big": 2**53 + 1}),
        "nested": ("s", {"x": {"y": ["z", {"w": 1}]}}),
    }

    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_hash_agrees_across_memory_disk_and_chain(self, tmp_path: Path, shape: str) -> None:
        scope, defaults = self._SHAPES[shape]
        chain = _chain(tmp_path)
        store = ConnectionDocumentStore(tmp_path / "conns")
        doc = create_document(
            name="c",
            broker_ref="GITHUB_TOKEN",
            scope=scope,
            connector_defaults=defaults,
            identity_dir=tmp_path / "id",
            chain=chain,
            store=store,
        )
        recorded = chain.query(event_type=EVENT_FLEET_CONN_CREATE)[0].details["document_hash"]
        from_disk = store.get("c")

        assert doc.document_hash() == from_disk.document_hash() == recorded
        assert from_disk == doc
        assert verify_document_local(from_disk, identity_dir=tmp_path / "id")

    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_signed_preimage_is_the_written_bytes_minus_the_signature(self, tmp_path: Path, shape: str) -> None:
        """The one relationship that makes the hash meaningful: what is signed
        is exactly what is stored, less the signature that cannot cover itself."""
        import json as _json

        scope, defaults = self._SHAPES[shape]
        store = ConnectionDocumentStore(tmp_path / "conns")
        doc = create_document(
            name="c",
            broker_ref="GITHUB_TOKEN",
            scope=scope,
            connector_defaults=defaults,
            identity_dir=tmp_path / "id",
            chain=_chain(tmp_path),
            store=store,
        )
        raw = (tmp_path / "conns" / "c.json").read_text(encoding="utf-8")
        # The writer must apply no normalisation of its own.
        assert raw == doc.to_json()

        written = _json.loads(raw)
        written.pop("signature")
        rebuilt = _json.dumps(written, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        assert rebuilt == doc.unsigned_canonical_bytes()
        assert rebuilt == store.get("c").unsigned_canonical_bytes()

    def test_rotation_hash_survives_the_storage_round_trip(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        store = ConnectionDocumentStore(tmp_path / "conns")
        create_document(
            name="c",
            broker_ref="TOK",
            scope="s",
            connector_defaults={"a": 1},
            identity_dir=tmp_path / "id",
            chain=chain,
            store=store,
        )
        rotated = rotate_document("c", new_broker_ref="TOK_V2", identity_dir=tmp_path / "id", chain=chain, store=store)
        recorded = chain.query(event_type=EVENT_FLEET_CONN_ROTATE)[0].details["new_document_hash"]
        from_disk = store.get("c")
        assert rotated.document_hash() == from_disk.document_hash() == recorded
        assert from_disk.version == 2
        assert verify_document_local(from_disk, identity_dir=tmp_path / "id")

    def test_put_does_not_mutate_the_document_it_persists(self, tmp_path: Path) -> None:
        """`put` validates before writing; validation must stay a pure check.
        A redaction here would make every recorded hash unverifiable."""
        store = ConnectionDocumentStore(tmp_path / "conns")
        doc = create_document(
            name="c",
            broker_ref="TOK",
            scope="s",
            connector_defaults={"a": 1},
            identity_dir=tmp_path / "id",
            chain=_chain(tmp_path),
            store=store,
        )
        before = doc.document_hash()
        store.put(doc)
        assert doc.document_hash() == before
        assert store.get("c").document_hash() == before
