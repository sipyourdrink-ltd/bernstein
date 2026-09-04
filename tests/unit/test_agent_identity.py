"""Tests for Agent Identity Lifecycle Management."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, get_args, get_type_hints

import pytest

from bernstein.core.identity.agent_jwt import (
    _CREDENTIAL_TOKEN_TYPES,
    AgentCredential,
    AgentIdentity,
    AgentIdentityStatus,
    AgentIdentityStore,
    IdentityAuditEvent,
    TokenType,
    _hash_token,
    permissions_for_role,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Permission tests
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_manager_has_spawn_permission(self) -> None:
        perms = permissions_for_role("manager")
        assert "agents:spawn" in perms
        assert "tasks:write" in perms

    def test_backend_has_file_write(self) -> None:
        perms = permissions_for_role("backend")
        assert "files:write" in perms
        assert "tests:run" in perms

    def test_qa_cannot_write_files(self) -> None:
        perms = permissions_for_role("qa")
        assert "files:write" not in perms
        assert "files:read" in perms

    def test_unknown_role_gets_defaults(self) -> None:
        perms = permissions_for_role("unknown-role")
        assert "tasks:read" in perms
        assert "files:read" in perms
        assert "agents:spawn" not in perms


# ---------------------------------------------------------------------------
# AgentCredential tests
# ---------------------------------------------------------------------------


class TestAgentCredential:
    def test_valid_credential(self) -> None:
        cred = AgentCredential(token_hash="abc123")
        assert cred.is_valid

    def test_revoked_credential_invalid(self) -> None:
        cred = AgentCredential(token_hash="abc123", revoked=True)
        assert not cred.is_valid

    def test_expired_credential_invalid(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=time.time() - 100)
        assert not cred.is_valid

    def test_future_expiry_valid(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=time.time() + 3600)
        assert cred.is_valid

    def test_zero_expiry_means_no_expiry(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=0.0)
        assert cred.is_valid

    def test_roundtrip_serialization(self) -> None:
        cred = AgentCredential(token_hash="abc123", expires_at=99999.0, revoked=False)
        restored = AgentCredential.from_dict(cred.to_dict())
        assert restored.token_hash == cred.token_hash
        assert restored.expires_at == cred.expires_at
        assert restored.revoked == cred.revoked


# ---------------------------------------------------------------------------
# AgentIdentity tests
# ---------------------------------------------------------------------------


class TestAgentIdentity:
    def test_active_identity_has_permission(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="backend",
            session_id="test-1",
            permissions=frozenset({"files:read", "files:write"}),
        )
        assert identity.has_permission("files:read")
        assert not identity.has_permission("agents:spawn")

    def test_revoked_identity_denies_all(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="backend",
            session_id="test-1",
            permissions=frozenset({"files:read"}),
            status=AgentIdentityStatus.REVOKED,
        )
        assert not identity.has_permission("files:read")

    def test_suspended_identity_denies_all(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="backend",
            session_id="test-1",
            permissions=frozenset({"files:read"}),
            status=AgentIdentityStatus.SUSPENDED,
        )
        assert not identity.has_permission("files:read")

    def test_is_active_property(self) -> None:
        active = AgentIdentity(id="a", role="backend", session_id="a")
        revoked = AgentIdentity(id="b", role="backend", session_id="b", status=AgentIdentityStatus.REVOKED)
        assert active.is_active
        assert not revoked.is_active

    def test_roundtrip_serialization(self) -> None:
        identity = AgentIdentity(
            id="test-1",
            role="security",
            session_id="test-1",
            permissions=frozenset({"files:read", "tests:run"}),
            parent_identity_id="parent-1",
            metadata={"cell_id": "cell-abc"},
            credential=AgentCredential(token_hash="hash123"),
        )
        restored = AgentIdentity.from_dict(identity.to_dict())
        assert restored.id == identity.id
        assert restored.role == identity.role
        assert restored.permissions == identity.permissions
        assert restored.parent_identity_id == "parent-1"
        assert restored.metadata == {"cell_id": "cell-abc"}
        assert restored.credential is not None
        assert restored.credential.token_hash == "hash123"

    def test_serialization_without_credential(self) -> None:
        identity = AgentIdentity(id="test-1", role="qa", session_id="test-1")
        data = identity.to_dict()
        assert data["credential"] is None
        restored = AgentIdentity.from_dict(data)
        assert restored.credential is None


# ---------------------------------------------------------------------------
# IdentityAuditEvent tests
# ---------------------------------------------------------------------------


class TestIdentityAuditEvent:
    def test_roundtrip(self) -> None:
        event = IdentityAuditEvent(
            timestamp=12345.0,
            identity_id="test-1",
            action="created",
            actor="spawner",
            details={"role": "backend"},
        )
        data = event.to_dict()
        assert data["identity_id"] == "test-1"
        assert data["action"] == "created"
        assert data["details"]["role"] == "backend"


# ---------------------------------------------------------------------------
# AgentIdentityStore tests
# ---------------------------------------------------------------------------


class TestAgentIdentityStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> AgentIdentityStore:
        return AgentIdentityStore(tmp_path)

    def test_create_identity(self, store: AgentIdentityStore) -> None:
        identity, token = store.create_identity("backend-abc123", "backend")
        assert identity.id == "backend-abc123"
        assert identity.role == "backend"
        assert identity.is_active
        assert "files:write" in identity.permissions
        assert len(token) > 0

    def test_create_with_extra_permissions(self, store: AgentIdentityStore) -> None:
        identity, _ = store.create_identity("mgr-1", "manager", extra_permissions=frozenset({"admin:override"}))
        assert "admin:override" in identity.permissions
        assert "agents:spawn" in identity.permissions

    def test_create_with_parent_identity(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager")
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id)
        assert child.parent_identity_id == "parent-1"

    def test_create_with_metadata(self, store: AgentIdentityStore) -> None:
        identity, _ = store.create_identity("s-1", "backend", metadata={"cell_id": "cell-x", "provider": "claude"})
        assert identity.metadata["cell_id"] == "cell-x"

    def test_authenticate_valid_token(self, store: AgentIdentityStore) -> None:
        _, token = store.create_identity("backend-abc", "backend")
        authed = store.authenticate(token)
        assert authed is not None
        assert authed.id == "backend-abc"
        assert authed.last_authenticated_at > 0

    def test_authenticate_invalid_token(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert store.authenticate("bad-token") is None

    def test_authenticate_revoked_identity(self, store: AgentIdentityStore) -> None:
        _, token = store.create_identity("backend-abc", "backend")
        store.revoke("backend-abc", reason="test")
        assert store.authenticate(token) is None

    def test_authorize_granted(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert store.authorize("backend-abc", "files:write")

    def test_authorize_denied(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert not store.authorize("backend-abc", "agents:spawn")

    def test_authorize_nonexistent(self, store: AgentIdentityStore) -> None:
        assert not store.authorize("no-such-id", "files:read")

    def test_revoke_identity(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        ok = store.revoke("backend-abc", reason="session ended")
        assert ok
        identity = store.get("backend-abc")
        assert identity is not None
        assert identity.status == AgentIdentityStatus.REVOKED
        assert identity.revoked_at > 0
        assert identity.revocation_reason == "session ended"
        assert identity.credential is not None
        assert identity.credential.revoked

    def test_revoke_nonexistent(self, store: AgentIdentityStore) -> None:
        assert not store.revoke("no-such-id")

    def test_revoke_and_suspend_logs_escape_newlines(
        self, store: AgentIdentityStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        store.create_identity("backend-abc", "backend")
        store.create_identity("backend-def", "backend")

        logger_name = "bernstein.core.identity.agent_jwt"
        with caplog.at_level("INFO", logger=logger_name):
            assert store.revoke("backend-abc", reason="line1\nline2")
            assert store.suspend("backend-def", reason="line3\rline4")

        messages = [record.getMessage() for record in caplog.records if record.name == logger_name]
        # Without this the four assertions below all pass vacuously on an empty list,
        # which is how a renamed logger slipped past them once already.
        assert messages, f"no records from {logger_name}; captured {[r.name for r in caplog.records]}"
        assert any("line1\\nline2" in message for message in messages)
        assert any("line3\\rline4" in message for message in messages)
        assert all("line1\nline2" not in message for message in messages)
        assert all("line3\rline4" not in message for message in messages)

    def test_suspend_and_reactivate(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        assert store.suspend("backend-abc", reason="investigation")
        identity = store.get("backend-abc")
        assert identity is not None
        assert identity.status == AgentIdentityStatus.SUSPENDED

        assert store.reactivate("backend-abc")
        identity = store.get("backend-abc")
        assert identity is not None
        assert identity.status == AgentIdentityStatus.ACTIVE

    def test_reactivate_revoked_fails(self, store: AgentIdentityStore) -> None:
        store.create_identity("backend-abc", "backend")
        store.revoke("backend-abc")
        assert not store.reactivate("backend-abc")

    def test_list_all_identities(self, store: AgentIdentityStore) -> None:
        store.create_identity("a-1", "backend")
        store.create_identity("b-2", "qa")
        store.create_identity("c-3", "security")
        identities = store.list_identities()
        assert len(identities) == 3

    def test_list_by_status(self, store: AgentIdentityStore) -> None:
        store.create_identity("a-1", "backend")
        store.create_identity("b-2", "qa")
        store.revoke("b-2")
        active = store.list_identities(status=AgentIdentityStatus.ACTIVE)
        revoked = store.list_identities(status=AgentIdentityStatus.REVOKED)
        assert len(active) == 1
        assert len(revoked) == 1
        assert active[0].id == "a-1"
        assert revoked[0].id == "b-2"

    def test_list_by_role(self, store: AgentIdentityStore) -> None:
        store.create_identity("a-1", "backend")
        store.create_identity("b-2", "qa")
        result = store.list_identities(role="qa")
        assert len(result) == 1
        assert result[0].role == "qa"

    def test_get_identity(self, store: AgentIdentityStore) -> None:
        store.create_identity("test-1", "backend")
        identity = store.get("test-1")
        assert identity is not None
        assert identity.id == "test-1"

    def test_get_nonexistent(self, store: AgentIdentityStore) -> None:
        assert store.get("no-such") is None

    def test_audit_trail(self, store: AgentIdentityStore) -> None:
        store.create_identity("test-1", "backend")
        store.authorize("test-1", "files:read")
        store.revoke("test-1", reason="done")
        trail = store.get_audit_trail("test-1")
        actions = [e.action for e in trail]
        assert "created" in actions
        assert "authorized" in actions
        assert "revoked" in actions

    def test_audit_trail_limit(self, store: AgentIdentityStore) -> None:
        store.create_identity("test-1", "backend")
        for _ in range(10):
            store.authorize("test-1", "files:read")
        trail = store.get_audit_trail("test-1", limit=3)
        assert len(trail) == 3

    def test_audit_trail_empty(self, store: AgentIdentityStore) -> None:
        trail = store.get_audit_trail("no-such")
        assert trail == []

    def test_token_with_expiry(self, store: AgentIdentityStore) -> None:
        identity, token = store.create_identity("s-1", "backend", token_expiry_s=3600)
        assert identity.credential is not None
        assert identity.credential.expires_at > 0
        authed = store.authenticate(token)
        assert authed is not None

    def test_persistence_across_store_instances(self, tmp_path: Path) -> None:
        store1 = AgentIdentityStore(tmp_path)
        store1.create_identity("persist-1", "backend")

        store2 = AgentIdentityStore(tmp_path)
        identity = store2.get("persist-1")
        assert identity is not None
        assert identity.id == "persist-1"

    def test_token_index_rebuilt_on_new_store(self, tmp_path: Path) -> None:
        store1 = AgentIdentityStore(tmp_path)
        _, token = store1.create_identity("persist-1", "backend")

        store2 = AgentIdentityStore(tmp_path)
        authed = store2.authenticate(token)
        assert authed is not None
        assert authed.id == "persist-1"

    def test_corrupt_identity_file_skipped(self, tmp_path: Path) -> None:
        store = AgentIdentityStore(tmp_path)
        corrupt_path = tmp_path / "agent_identities" / "corrupt.json"
        corrupt_path.write_text("not json", encoding="utf-8")
        identities = store.list_identities()
        assert len(identities) == 0


# ---------------------------------------------------------------------------
# hash_token tests
# ---------------------------------------------------------------------------


class TestHashToken:
    def test_deterministic(self) -> None:
        h1 = _hash_token("my-secret-token")
        h2 = _hash_token("my-secret-token")
        assert h1 == h2

    def test_different_tokens_different_hashes(self) -> None:
        h1 = _hash_token("token-a")
        h2 = _hash_token("token-b")
        assert h1 != h2


class TestAgentCredentialTenantDeserialization:
    """The persisted ``tenant_id`` becomes the authenticated request scope.

    ``AgentCredential.from_dict`` is the boundary where a stored record turns
    into that scope, so it establishes the value as a real tenant id instead
    of coercing whatever shape was on disk into a usable one.
    """

    def test_absent_tenant_resolves_to_the_default(self) -> None:
        """Records written before the field existed still load."""
        credential = AgentCredential.from_dict({"token_hash": "abc"})

        assert credential.tenant_id == "default"

    def test_named_tenant_is_preserved(self) -> None:
        credential = AgentCredential.from_dict({"token_hash": "abc", "tenant_id": "tenant-a"})

        assert credential.tenant_id == "tenant-a"

    @pytest.mark.parametrize("stored", [123, 1.5, True, ["tenant-a"], {"id": "tenant-a"}])
    def test_non_string_tenant_is_refused(self, stored: object) -> None:
        """A non-string record is rejected rather than stringified.

        ``str()`` would turn any of these into a non-blank value that reads
        back as a legitimate scope.
        """
        with pytest.raises(ValueError, match="tenant_id"):
            AgentCredential.from_dict({"token_hash": "abc", "tenant_id": stored})

    @pytest.mark.parametrize("stored", ["", "   ", "\t"])
    def test_blank_tenant_is_refused(self, stored: str) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            AgentCredential.from_dict({"token_hash": "abc", "tenant_id": stored})

    def test_store_skips_an_identity_whose_credential_is_refused(self, tmp_path: Path) -> None:
        """A record that cannot establish a scope is skipped, not listed.

        ``list_identities`` reports what the store can vouch for; an identity
        whose credential does not deserialise is left out entirely rather
        than surfaced with a scope derived from the bad record.
        """
        import json

        store = AgentIdentityStore(tmp_path)
        identity, _token = store.create_identity("session-good", "backend")

        identities_dir = tmp_path / "agent_identities"
        corrupt = identities_dir / "session-corrupt.json"
        payload = json.loads((identities_dir / f"{identity.id}.json").read_text())
        payload["id"] = "session-corrupt"
        payload["session_id"] = "session-corrupt"
        payload["credential"]["tenant_id"] = 42
        corrupt.write_text(json.dumps(payload))

        listed = {found.id for found in store.list_identities()}

        assert identity.id in listed
        assert "session-corrupt" not in listed


class TestAgentCredentialTokenTypeDeserialization:
    """The persisted ``token_type`` selects which validation a token gets.

    ``_validate_jwt_claims`` refuses any credential that does not say
    ``"jwt"``, so an unrecognised kind is not inert - it routes the token to
    the opaque hash comparison instead of being refused.  ``from_dict``
    establishes the value as one of the two real kinds rather than coercing
    whatever was on disk into one that some comparison will happen to accept.
    """

    def test_absent_token_type_resolves_to_opaque(self) -> None:
        """Records written before the field existed still load."""
        credential = AgentCredential.from_dict({"token_hash": "abc"})

        assert credential.token_type == "opaque"

    @pytest.mark.parametrize("stored", ["opaque", "jwt"])
    def test_both_kinds_round_trip_unchanged(self, stored: str) -> None:
        credential = AgentCredential.from_dict({"token_hash": "abc", "token_type": stored})

        assert credential.token_type == stored
        assert AgentCredential.from_dict(credential.to_dict()).token_type == stored

    @pytest.mark.parametrize("stored", ["anything", "JWT", "opaque ", "", None, 1, True, [], {}])
    def test_unknown_token_type_is_refused_and_named(self, stored: object) -> None:
        """The message names the offending value, so the record is findable.

        A store holding one bad record is repaired by locating it; a refusal
        that only names the field leaves every credential a suspect.

        The last two cases are the reason the membership test is guarded by
        ``isinstance``: a list and an object are the only two values JSON can
        persist that a set lookup cannot hash, so without the guard they
        raise ``TypeError: unhashable type`` from the lookup rather than the
        named ``ValueError``.  Both are here rather than one, because they
        are two different unhashable shapes and a guard covering one is not
        evidence about the other.
        """
        with pytest.raises(ValueError, match="token_type") as excinfo:
            AgentCredential.from_dict({"token_hash": "abc", "token_type": stored})

        assert repr(stored) in str(excinfo.value)

    def test_unknown_token_type_does_not_authenticate(self, tmp_path: Path) -> None:
        """The refusal reaches authentication as a miss, not as a 500.

        Before this change the record loaded, ``token_type != "jwt"`` sent it
        down the opaque branch, and the identity authenticated on its token
        hash alone.  The refusal now travels the same path an unusable
        ``tenant_id`` already does - through ``_load``, which skips the file.
        """
        import json

        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-jwt", "backend")
        path = tmp_path / "agent_identities" / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        assert payload["credential"]["token_type"] == "jwt"
        payload["credential"]["token_type"] = "anything"
        path.write_text(json.dumps(payload))

        assert store.authenticate(token) is None
        assert [found.id for found in store.list_identities()] == []


class TestTokenTypeAllowlistIsDerivedNotRestated:
    """The allowlist and the annotation are one source of truth, not two.

    The refactor these tests guard (#4015) replaced a hand-written
    ``frozenset`` with :func:`typing.get_args` over :data:`TokenType`. The
    behaviour is unchanged, so the deserialisation tests above cannot tell
    the two apart - they pass either way. What is new is the *property*, and
    a property nothing asserts is one refactor away from being lost again.

    The failure being pinned is quiet: restate the pair on the annotation,
    add a kind there, and the type permits a value the boundary refuses.
    Nothing raises, mypy stays green, and the disagreement only surfaces at
    the far end of a stack trace.
    """

    def test_the_runtime_allowlist_comes_from_the_type(self) -> None:
        """A hand-written copy would satisfy today and drift tomorrow."""
        assert tuple(_CREDENTIAL_TOKEN_TYPES) == get_args(TokenType)

    def test_the_field_annotation_denotes_the_same_type(self) -> None:
        """The field and the allowlist must not come to permit different sets.

        Worth being exact about what this catches, since ``Literal`` interns
        its instances: a field that spells the same two values out again *is*
        :data:`TokenType`, and this passes. That is not a gap - an identical
        restatement denotes the same type and cannot disagree with it.

        What it catches is the edit that matters. Add a kind to the field and
        not to :data:`TokenType` and the annotations are different objects,
        so this fails - which is the case where the type permits a value the
        boundary refuses.
        """
        annotation = get_type_hints(AgentCredential)["token_type"]

        assert annotation is TokenType

    def test_every_declared_kind_is_admitted(self) -> None:
        """Whatever the type permits, the boundary accepts - by construction.

        Parametrising over ``get_args`` rather than a written-out list is the
        point: a kind added to :data:`TokenType` is covered here the moment
        it exists, without anyone remembering to extend this test.
        """
        for kind in get_args(TokenType):
            credential = AgentCredential.from_dict({"token_hash": "abc", "token_type": kind})

            assert credential.token_type == kind


class TestCorruptIdentityDoesNotBreakAuthentication:
    """A record that will not deserialise authenticates as nobody, not as a 500.

    ``AgentCredential.from_dict`` refuses a persisted ``tenant_id`` that is
    not a real tenant id, and that refusal reaches every caller through
    ``AgentIdentityStore._load``.  ``_load`` sits under the authentication
    entry points, which sit under the server's auth middleware, so an escape
    there turns an unusable stored record into a 500 on an unauthenticated
    request - a corrupt file on disk becoming a server error any caller can
    trigger. The store answers "no such identity" instead.
    """

    @staticmethod
    def _corrupt_tenant(identities_dir: Path, identity_id: str) -> None:
        """Rewrite one persisted record's credential tenant to a bad value."""
        import json

        path = identities_dir / f"{identity_id}.json"
        payload = json.loads(path.read_text())
        payload["credential"]["tenant_id"] = 42
        path.write_text(json.dumps(payload))

    def test_jwt_authentication_returns_none_for_a_corrupt_record(self, tmp_path: Path) -> None:
        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-jwt", "backend")
        assert store.authenticate(token) is not None, "precondition: the token authenticates before corruption"

        self._corrupt_tenant(tmp_path / "agent_identities", identity.id)

        assert AgentIdentityStore(tmp_path).authenticate(token) is None

    def test_opaque_authentication_returns_none_for_a_corrupt_record(self, tmp_path: Path) -> None:
        """The opaque-token path reaches ``_load`` through the token index.

        The index is rebuilt from the raw JSON and never validates the
        tenant, so it hands out the identity id and the failure lands in
        ``_load`` - a different route to the same record than the JWT path.
        """
        import json

        opaque_token = "opaque-agent-token"
        store = AgentIdentityStore(tmp_path)
        identity, _token = store.create_identity("session-opaque", "backend")

        identities_dir = tmp_path / "agent_identities"
        path = identities_dir / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        payload["credential"]["token_type"] = "opaque"
        payload["credential"]["token_hash"] = _hash_token(opaque_token)
        path.write_text(json.dumps(payload))
        assert AgentIdentityStore(tmp_path).authenticate(opaque_token) is not None, (
            "precondition: the opaque token authenticates before corruption"
        )

        self._corrupt_tenant(identities_dir, identity.id)

        assert AgentIdentityStore(tmp_path).authenticate(opaque_token) is None

    def test_a_valid_identity_still_authenticates_alongside_a_corrupt_one(self, tmp_path: Path) -> None:
        """Skipping the bad record does not take the good ones with it."""
        import json

        store = AgentIdentityStore(tmp_path)
        good, good_token = store.create_identity("session-good", "backend")

        identities_dir = tmp_path / "agent_identities"
        corrupt = identities_dir / "session-corrupt.json"
        payload = json.loads((identities_dir / f"{good.id}.json").read_text())
        payload["id"] = "session-corrupt"
        payload["session_id"] = "session-corrupt"
        payload["credential"]["token_hash"] = _hash_token("some-other-token")
        payload["credential"]["tenant_id"] = 42
        corrupt.write_text(json.dumps(payload))

        reloaded = AgentIdentityStore(tmp_path)

        assert reloaded.get("session-corrupt") is None
        authenticated = reloaded.authenticate(good_token)
        assert authenticated is not None
        assert authenticated.id == good.id


class TestIdentityReadersSkipCorruptFilesIdentically:
    """Every reader of the identity directory survives the same bad file.

    The store has three readers - the startup token-index scan, the listing
    route, and the by-id lookup behind authentication - and each used to pick
    the JSON apart with its own exception list.  A file that is not an object
    at all made the startup scan raise `AttributeError` and refuse to
    construct the store; a malformed `metadata` or `permissions` value made
    the listing route raise `TypeError` out of `GET /identities`.  All three
    now share one validated reader, so a bad file is skipped everywhere.
    """

    @staticmethod
    def _write(identities_dir: Path, name: str, raw: str) -> None:
        (identities_dir / f"{name}.json").write_text(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "null",
            "42",
            '"a string, not a record"',
            "[]",
            '{"id": "x", "role": "backend", "session_id": "x", "credential": 5}',
            "{not json at all",
        ],
    )
    def test_store_constructs_despite_a_malformed_file(self, tmp_path: Path, raw: str) -> None:
        """A malformed file must not stop the store from being built.

        ``_rebuild_token_index`` runs from ``__init__``, so anything escaping
        it takes the server down at startup rather than degrading one record.
        """
        store = AgentIdentityStore(tmp_path)
        _identity, token = store.create_identity("session-good", "backend")
        self._write(tmp_path / "agent_identities", "session-broken", raw)

        reloaded = AgentIdentityStore(tmp_path)

        assert reloaded.authenticate(token) is not None
        assert reloaded.get("session-broken") is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("metadata", 7), ("permissions", 7), ("status", "not-a-status")],
    )
    def test_listing_skips_a_record_with_a_malformed_field(self, tmp_path: Path, field: str, value: object) -> None:
        """``GET /identities`` lists what it can vouch for and skips the rest."""
        import json

        store = AgentIdentityStore(tmp_path)
        good, _token = store.create_identity("session-good", "backend")

        identities_dir = tmp_path / "agent_identities"
        payload = json.loads((identities_dir / f"{good.id}.json").read_text())
        payload["id"] = "session-broken"
        payload["session_id"] = "session-broken"
        payload[field] = value
        (identities_dir / "session-broken.json").write_text(json.dumps(payload))

        listed = {found.id for found in AgentIdentityStore(tmp_path).list_identities()}

        assert good.id in listed
        assert "session-broken" not in listed


class TestExplicitNullTenantIsRefused:
    """An omitted tenant is the legacy case; an explicit null is not.

    Leniency exists for records written before the field existed, which carry
    no key at all.  A record that carries the key with `null` in it asserted a
    scope and asserted a non-scope, so treating it as the legacy case would
    authenticate it under the default tenant on the strength of a value that
    is not a tenant.
    """

    def test_absent_key_still_resolves_to_the_default(self) -> None:
        assert AgentCredential.from_dict({"token_hash": "abc"}).tenant_id == "default"

    def test_explicit_null_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            AgentCredential.from_dict({"token_hash": "abc", "tenant_id": None})

    def test_explicit_null_does_not_authenticate(self, tmp_path: Path) -> None:
        """The refusal reaches authentication as a miss, not as a 500."""
        import json

        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-null", "backend")
        path = tmp_path / "agent_identities" / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        payload["credential"]["tenant_id"] = None
        path.write_text(json.dumps(payload))

        assert AgentIdentityStore(tmp_path).authenticate(token) is None


class TestPersistedScopeCollectionsAreValidated:
    """Permissions and scopes are authorization, so their shape is checked.

    ``frozenset()`` and ``list()`` take any iterable, which is the wrong
    behaviour for reading a stored grant: a mapping yields its keys, so a
    corrupt ``permissions`` object hands out real permissions, and a mapping
    in ``task_ids`` collapses to an empty list - which does not mean "no
    tasks" but "no restriction".
    """

    def test_a_mapping_does_not_become_held_permissions(self) -> None:
        """The keys of a mapping are not a grant."""
        with pytest.raises(ValueError, match="permissions"):
            AgentIdentity.from_dict(
                {
                    "id": "x",
                    "role": "backend",
                    "session_id": "x",
                    "permissions": {"admin:manage": 1},
                }
            )

    def test_a_string_does_not_become_per_character_permissions(self) -> None:
        with pytest.raises(ValueError, match="permissions"):
            AgentIdentity.from_dict({"id": "x", "role": "backend", "session_id": "x", "permissions": "admin:manage"})

    @pytest.mark.parametrize("field", ["task_ids", "allowed_files"])
    @pytest.mark.parametrize("value", [{}, {"t-1": True}, "t-1", 7, None])
    def test_a_scope_field_that_is_not_a_list_is_refused(self, field: str, value: object) -> None:
        """An empty scope means unrestricted, so a bad shape must not reach it."""
        with pytest.raises(ValueError, match=field):
            AgentIdentity.from_dict({"id": "x", "role": "backend", "session_id": "x", field: value})

    @pytest.mark.parametrize("field", ["task_ids", "allowed_files"])
    def test_a_non_string_entry_is_refused(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            AgentIdentity.from_dict({"id": "x", "role": "backend", "session_id": "x", field: ["ok", 7]})

    def test_credential_scopes_are_validated_too(self) -> None:
        """The credential carries its own copy of the task scope."""
        with pytest.raises(ValueError, match="task_ids"):
            AgentCredential.from_dict({"token_hash": "abc", "task_ids": {"t-1": True}})

    def test_well_formed_records_still_round_trip(self, tmp_path: Path) -> None:
        """Validation does not reject what the store actually writes."""
        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-ok", "backend", task_ids=["t-1", "t-2"])

        reloaded = AgentIdentityStore(tmp_path).authenticate(token)

        assert reloaded is not None
        assert reloaded.task_ids == ["t-1", "t-2"]
        assert reloaded.permissions == identity.permissions

    def test_a_record_with_a_corrupt_scope_does_not_authenticate(self, tmp_path: Path) -> None:
        """The refusal reaches authentication as a miss, not as a wide grant."""
        import json

        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-scope", "backend", task_ids=["t-1"])
        path = tmp_path / "agent_identities" / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        # An empty mapping would deserialise to [] - "no task restriction".
        payload["credential"]["task_ids"] = {}
        payload["task_ids"] = {}
        path.write_text(json.dumps(payload))

        assert AgentIdentityStore(tmp_path).authenticate(token) is None

    @pytest.mark.parametrize("field", ["task_ids", "allowed_files"])
    @pytest.mark.parametrize("value", [["ok", 7], ["ok", None], "t-1", {"t-1": True}, {}, "", 0, False])
    def test_a_bad_scope_is_refused_before_a_token_is_minted(self, tmp_path: Path, field: str, value: object) -> None:
        """A scope the reader would refuse must not become a signed credential.

        Validating only on the read side leaves the caller able to mint a
        token whose own record cannot be loaded - the agent then authenticates
        as an unknown identity for the whole life of the token.
        """
        store = AgentIdentityStore(tmp_path)

        with pytest.raises(ValueError, match=field):
            store.create_identity("session-bad-scope", "backend", **{field: value})  # type: ignore[arg-type]

        assert not list((tmp_path / "agent_identities").glob("session-bad-scope*.json"))

    @pytest.mark.parametrize("field", ["task_ids", "allowed_files"])
    @pytest.mark.parametrize("value", [None, []])
    def test_an_absent_or_empty_scope_still_means_unrestricted(self, tmp_path: Path, field: str, value: object) -> None:
        """Refusing falsy junk must not also refuse the two real "no scope" values.

        ``None`` is the argument's default and an empty list is a caller
        explicitly asking for no restriction.  Both stay valid; it is the other
        falsy shapes that are refused rather than read as "unrestricted".
        """
        store = AgentIdentityStore(tmp_path)

        identity, token = store.create_identity("session-open-scope", "backend", **{field: value})  # type: ignore[arg-type]

        assert getattr(identity, field) == []
        assert AgentIdentityStore(tmp_path).authenticate(token) is not None

    def test_a_legacy_shaped_record_is_refused_before_its_claims_are_read(self, tmp_path: Path) -> None:
        """A stored non-string scope dies at the read, not at the claim comparison.

        Both copies of the scope are written together, so a token can only
        carry a non-string entry if the record beside it carries one too - and
        that record is refused when it loads, a step before the claim check
        runs.  Tightening the claim comparison therefore takes no token out of
        service that was still in service without it.
        """
        import json

        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-legacy", "backend", task_ids=["7"])
        path = tmp_path / "agent_identities" / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        payload["task_ids"] = [7]
        payload["credential"]["task_ids"] = [7]
        path.write_text(json.dumps(payload))

        reloaded = AgentIdentityStore(tmp_path)

        assert reloaded._load(identity.id) is None
        assert reloaded.authenticate(token) is None


class TestIdentityAndCredentialScopeMustAgree:
    """Two copies of the task scope must not disagree about what is allowed.

    ``task_ids`` and ``allowed_files`` are persisted on the identity and on
    its credential.  Different consumers read different copies - the request
    middleware reads the identity's, the JWT claim check reads the
    credential's - so a record holding two answers is authorized under
    whichever copy the reader happens to reach.  An identity holding an empty
    list beside a scoped credential is the dangerous direction: empty means
    unrestricted.
    """

    @pytest.mark.parametrize("field", ["task_ids", "allowed_files"])
    def test_an_empty_identity_scope_beside_a_scoped_credential_is_refused(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            AgentIdentity.from_dict(
                {
                    "id": "x",
                    "role": "backend",
                    "session_id": "x",
                    "credential": {"token_hash": "abc", field: ["t-1"]},
                    field: [],
                }
            )

    @pytest.mark.parametrize("field", ["task_ids", "allowed_files"])
    def test_a_wider_identity_scope_is_refused(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            AgentIdentity.from_dict(
                {
                    "id": "x",
                    "role": "backend",
                    "session_id": "x",
                    "credential": {"token_hash": "abc", field: ["t-1"]},
                    field: ["t-1", "t-2"],
                }
            )

    def test_matching_scopes_in_any_order_are_accepted(self) -> None:
        """Ordering is not a scope difference - only membership is."""
        identity = AgentIdentity.from_dict(
            {
                "id": "x",
                "role": "backend",
                "session_id": "x",
                "credential": {"token_hash": "abc", "task_ids": ["t-2", "t-1"]},
                "task_ids": ["t-1", "t-2"],
            }
        )

        assert identity.task_ids == ["t-1", "t-2"]

    def test_an_identity_without_a_credential_is_unaffected(self) -> None:
        identity = AgentIdentity.from_dict({"id": "x", "role": "backend", "session_id": "x", "task_ids": ["t-1"]})

        assert identity.task_ids == ["t-1"]

    def test_a_divergent_record_does_not_authenticate_an_opaque_token(self, tmp_path: Path) -> None:
        """An opaque token never reaches the claim check that would disagree.

        The JWT path compares the token's claims against the credential, so a
        widened identity copy is caught there.  An opaque token skips that
        comparison entirely, which is exactly the case the read-side check has
        to cover.
        """
        import json

        store = AgentIdentityStore(tmp_path)
        identity, _ = store.create_identity("session-opaque", "backend", task_ids=["t-1"])
        path = tmp_path / "agent_identities" / f"{identity.id}.json"
        payload = json.loads(path.read_text())
        opaque_token = "opaque-token-value"
        payload["credential"]["token_type"] = "opaque"
        payload["credential"]["token_hash"] = _hash_token(opaque_token)
        # The credential stays scoped; the identity copy is widened to
        # "unrestricted", which is what the request middleware reads.
        payload["task_ids"] = []
        path.write_text(json.dumps(payload))

        assert AgentIdentityStore(tmp_path).authenticate(opaque_token) is None

    def test_what_the_store_writes_still_loads(self, tmp_path: Path) -> None:
        store = AgentIdentityStore(tmp_path)
        _, token = store.create_identity("session-agree", "backend", task_ids=["t-1"], allowed_files=["src/a.py"])

        reloaded = AgentIdentityStore(tmp_path).authenticate(token)

        assert reloaded is not None
        assert reloaded.task_ids == ["t-1"]
        assert reloaded.allowed_files == ["src/a.py"]


class TestSignedClaimsAreComparedWithoutCoercion:
    """A claim is compared as stored, not as ``str()`` renders it.

    ``sorted(map(str, claim))`` makes a claim of ``[1]`` compare equal to a
    stored ``["1"]``.  The comparison exists to establish that the token's
    scope is the credential's scope, and a coerced match does not establish
    that.
    """

    @staticmethod
    def _claims_for(identity: AgentIdentity, **overrides: object) -> dict[str, object]:
        cred = identity.credential
        assert cred is not None
        return {
            "sub": identity.id,
            "sid": identity.session_id,
            "role": identity.role,
            "jti": cred.jti,
            "scopes": sorted(identity.permissions),
            "tenant_id": cred.tenant_id,
            "task_ids": list(cred.task_ids),
            "allowed_files": list(cred.allowed_files),
            **overrides,
        }

    @pytest.mark.parametrize(
        ("field", "stored", "claimed"),
        [
            ("task_ids", ["1"], [1]),
            ("task_ids", ["True"], [True]),
            ("allowed_files", ["1"], [1]),
            ("scopes", None, [1]),
        ],
    )
    def test_a_coercible_claim_is_not_accepted_as_a_match(
        self, tmp_path: Path, field: str, stored: list[str] | None, claimed: list[object]
    ) -> None:
        """``str()`` on a claim would make a number match a stored string."""
        store = AgentIdentityStore(tmp_path)
        task_ids = stored if field == "task_ids" else None
        allowed_files = stored if field == "allowed_files" else None
        identity, token = store.create_identity(
            f"session-claims-{field}", "backend", task_ids=task_ids, allowed_files=allowed_files
        )
        claims = self._claims_for(identity, **{field: claimed})

        assert store._validate_jwt_claims(claims, identity, token) is False

    def test_a_claim_that_is_not_a_list_is_not_accepted(self, tmp_path: Path) -> None:
        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity("session-claims-shape", "backend", task_ids=["t-1"])
        claims = self._claims_for(identity, task_ids={"t-1": True})

        assert store._validate_jwt_claims(claims, identity, token) is False

    def test_the_issued_claims_still_validate(self, tmp_path: Path) -> None:
        """The tightened comparison does not reject a real token."""
        store = AgentIdentityStore(tmp_path)
        identity, token = store.create_identity(
            "session-claims-ok", "backend", task_ids=["t-1"], allowed_files=["src/a.py"]
        )

        assert store._validate_jwt_claims(self._claims_for(identity), identity, token) is True
        assert AgentIdentityStore(tmp_path).authenticate(token) is not None


# ---------------------------------------------------------------------------
# Child scope must narrow the parent's, never widen it
# ---------------------------------------------------------------------------


class TestChildScopeMustNarrowTheParents:
    """A child identity may not hold a scope its parent never held.

    ``parent_identity_id`` was recorded on the identity, serialised, audited
    and displayed, but never compared against anything: a caller naming a
    parent could mint a child scoped to tasks and files the parent itself was
    refused.  ``create_identity`` now refuses that at declaration, before a
    token is signed, so a widened scope cannot become a credential.

    An empty scope means *unrestricted* on both sides of the comparison. An
    unrestricted parent may mint anything; a restricted parent may not mint an
    unrestricted child.
    """

    @pytest.fixture()
    def store(self, tmp_path: Path) -> AgentIdentityStore:
        return AgentIdentityStore(tmp_path)

    # -- task_ids ---------------------------------------------------------

    def test_a_task_the_parent_does_not_hold_is_refused(self, store: AgentIdentityStore) -> None:
        """The refusal names the axis and both sides of the comparison."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2"])
        with pytest.raises(
            ValueError,
            match=r"child task_ids \['t-1', 't-2', 't-9'\] are not a subset of parent task_ids \['t-1', 't-2'\]",
        ):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=["t-1", "t-2", "t-9"])

    def test_a_narrower_task_set_is_accepted(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2", "t-3"])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=["t-1", "t-2"])
        assert child.task_ids == ["t-1", "t-2"]

    def test_the_same_task_set_is_accepted(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1", "t-2"])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=["t-1", "t-2"])
        assert child.task_ids == ["t-1", "t-2"]

    def test_an_unrestricted_parent_may_mint_any_task_scope(self, store: AgentIdentityStore) -> None:
        """Empty parent ``task_ids`` means unrestricted, so nothing is narrowed."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=[])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=["t-1"])
        assert child.task_ids == ["t-1"]

    def test_a_restricted_parent_may_mint_an_empty_task_scope(self, store: AgentIdentityStore) -> None:
        """No task scope narrows to nothing, which is not a widening."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1"])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=[])
        assert child.task_ids == []

    # -- allowed_files ----------------------------------------------------

    def test_a_file_the_parent_does_not_hold_is_refused(self, store: AgentIdentityStore) -> None:
        """The refusal names the axis and both sides of the comparison."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py"])
        with pytest.raises(
            ValueError,
            match=(
                r"child allowed_files \['src/a.py', 'src/b.py', 'src/c.py'\] are not a subset of "
                r"parent allowed_files \['src/a.py', 'src/b.py'\]"
            ),
        ):
            store.create_identity(
                "child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py", "src/b.py", "src/c.py"]
            )

    def test_a_narrower_file_scope_is_accepted(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py", "src/c.py"])
        child, _ = store.create_identity(
            "child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py", "src/b.py"]
        )
        assert child.allowed_files == ["src/a.py", "src/b.py"]

    def test_the_same_file_scope_is_accepted(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py", "src/b.py"])
        child, _ = store.create_identity(
            "child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py", "src/b.py"]
        )
        assert child.allowed_files == ["src/a.py", "src/b.py"]

    def test_a_file_outside_the_parents_tree_is_refused(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/**"])
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["tests/a.py"])

    def test_an_empty_file_scope_under_a_restricted_parent_is_refused(self, store: AgentIdentityStore) -> None:
        """Empty means unrestricted, so it widens rather than narrows."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/a.py"])
        with pytest.raises(
            ValueError, match=r"child allowed_files \[\] are not a subset of parent allowed_files \['src/a.py'\]"
        ):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=[])

    def test_an_unrestricted_parent_may_mint_an_unrestricted_child(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=[])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=[])
        assert child.allowed_files == []

    # -- coverage is decided by the merge gate's matcher, not by string prefix

    def test_a_tree_glob_covers_the_files_beneath_it(self, store: AgentIdentityStore) -> None:
        """``src/**`` is how a tree is admitted, so files under it narrow it."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/**"])
        child, _ = store.create_identity(
            "child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py", "src/b/c.py"]
        )
        assert child.allowed_files == ["src/a.py", "src/b/c.py"]

    def test_a_bare_directory_does_not_cover_the_files_beneath_it(self, store: AgentIdentityStore) -> None:
        """``src`` admits the path ``src`` and nothing under it.

        Read as a string prefix instead, a parent scoped to ``src`` would mint a
        child scoped to ``src/secret.py`` -- a file the merge gate never admits
        for the parent.  The two surfaces have to answer this the same way.
        """
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src"])
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/secret.py"])

    def test_a_single_star_covers_one_segment_only(self, store: AgentIdentityStore) -> None:
        """``src/*`` admits ``src/a.py`` but not ``src/a/b.py``."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/*"])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/a.py"])
        assert child.allowed_files == ["src/a.py"]

        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-2", "backend", parent_identity_id=parent.id, allowed_files=["src/a/b.py"])

    def test_a_sibling_directory_sharing_a_prefix_is_not_covered(self, store: AgentIdentityStore) -> None:
        """``src/b`` must not cover ``src/bc.py``: coverage is by segment."""
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/b/**"])
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/bc.py"])

    def test_a_child_glob_the_parent_did_not_declare_is_refused(self, store: AgentIdentityStore) -> None:
        """A child that is itself a glob is only admitted when the parent declared it.

        ``src/**`` names files ``src/*`` does not, so accepting it under that
        parent would widen the scope.  Containment between two globs is not
        decided here; the refusal is the direction that cannot widen.
        """
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/*"])
        with pytest.raises(ValueError, match="child allowed_files .* are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/**"])

    def test_a_glob_the_parent_declared_is_accepted(self, store: AgentIdentityStore) -> None:
        parent, _ = store.create_identity("parent-1", "manager", allowed_files=["src/**", "docs/**"])
        child, _ = store.create_identity("child-1", "backend", parent_identity_id=parent.id, allowed_files=["src/**"])
        assert child.allowed_files == ["src/**"]

    # -- the surrounding contract ----------------------------------------

    def test_a_parent_that_does_not_exist_is_refused(self, store: AgentIdentityStore) -> None:
        """The scope cannot be compared, so no token is signed."""
        with pytest.raises(ValueError, match="parent identity no-such-parent not found"):
            store.create_identity("child-1", "backend", parent_identity_id="no-such-parent", task_ids=["t-1"])

    def test_an_identity_with_no_parent_is_unaffected(self, store: AgentIdentityStore) -> None:
        identity, _ = store.create_identity("solo-1", "backend", task_ids=["t-1"], allowed_files=["src/a.py"])
        assert identity.parent_identity_id is None
        assert identity.task_ids == ["t-1"]

    def test_a_refused_child_leaves_no_identity_behind(self, store: AgentIdentityStore) -> None:
        """Refused at declaration, so nothing is persisted and no token exists."""
        parent, _ = store.create_identity("parent-1", "manager", task_ids=["t-1"])
        with pytest.raises(ValueError, match="are not a subset of"):
            store.create_identity("child-1", "backend", parent_identity_id=parent.id, task_ids=["t-9"])

        assert store.get("child-1") is None
