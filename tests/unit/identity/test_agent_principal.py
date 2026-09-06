"""One identity type behind both agent credential formats.

An authority decision made against a JWT-authenticated agent used to be
uncheckable against an Ed25519-carded one: the two lived in unrelated types
with no shared id space. These tests pin the single type both formats resolve
to, the ids the real mint paths produce for it, and the id a delegation
receipt records for a principal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.identity.agent_card import AgentIdentityCard, issue_identity_card
from bernstein.core.identity.agent_jwt import (
    AgentCredential,
    AgentIdentity,
    AgentIdentityStatus,
    AgentIdentityStore,
)
from bernstein.core.identity.delegation import DelegationLedger
from bernstein.core.identity.principal import (
    AgentPrincipal,
    CredentialFormat,
    CredentialRef,
    PrincipalStatus,
    principal_from_agent_identity,
    principal_from_identity_card,
    principal_ref,
)

if TYPE_CHECKING:
    from pathlib import Path

AGENT_ID = "agent-7f3c9d"


def _jwt_identity(**overrides: object) -> AgentIdentity:
    credential = AgentCredential(
        token_hash="a" * 64,
        created_at=1_000.0,
        expires_at=0.0,
        token_type="jwt",
        algorithm="HS256",
        jti="jti-0001",
        tenant_id="acme",
    )
    fields: dict[str, object] = {
        "id": AGENT_ID,
        "role": "backend",
        "session_id": "sess-42",
        "permissions": frozenset({"tasks:read"}),
        "credential": credential,
    }
    fields.update(overrides)
    return AgentIdentity(**fields)  # type: ignore[arg-type]


def _card(**overrides: object) -> AgentIdentityCard:
    fields: dict[str, object] = {
        "agent_id": AGENT_ID,
        "role": "backend",
        "adapter": "claude_code",
        "model": "opus",
        "created_at": 1_000.0,
    }
    fields.update(overrides)
    return AgentIdentityCard(**fields)  # type: ignore[arg-type]


def test_jwt_and_ed25519_credentials_resolve_to_one_identity() -> None:
    """Load-bearing: both credential formats produce the same principal id."""
    from_jwt = principal_from_agent_identity(_jwt_identity())
    from_card = principal_from_identity_card(_card())

    assert isinstance(from_jwt, AgentPrincipal)
    assert isinstance(from_card, AgentPrincipal)
    assert from_jwt.id == from_card.id == AGENT_ID

    joined = from_jwt.merge(from_card)
    jwt_credential = joined.credential_for(CredentialFormat.JWT)
    card_credential = joined.credential_for(CredentialFormat.ED25519_CARD)
    assert jwt_credential is not None
    assert card_credential is not None
    assert jwt_credential.ref == "jti-0001"
    assert card_credential.ref == _card().card_hash


def test_jwt_credential_is_referenced_by_its_jti_not_its_token_hash() -> None:
    principal = principal_from_agent_identity(_jwt_identity())
    credential = principal.credential_for(CredentialFormat.JWT)
    assert credential is not None
    assert credential.ref == "jti-0001"
    assert credential.algorithm == "HS256"
    assert principal.tenant_id == "acme"


def test_opaque_store_credential_is_not_reported_as_jwt() -> None:
    opaque = AgentCredential(token_hash="b" * 64, token_type="opaque", algorithm="HS256")
    principal = principal_from_agent_identity(_jwt_identity(credential=opaque))

    assert principal.credential_for(CredentialFormat.JWT) is None
    opaque_ref = principal.credential_for(CredentialFormat.OPAQUE)
    assert opaque_ref is not None
    assert opaque_ref.ref == "b" * 64
    assert opaque_ref.algorithm == ""


def test_card_credential_is_referenced_by_card_hash() -> None:
    card = _card()
    principal = principal_from_identity_card(card)
    credential = principal.credential_for(CredentialFormat.ED25519_CARD)
    assert credential is not None
    assert credential.ref == card.card_hash
    assert credential.issued_at == 1_000.0


def test_unsigned_card_declares_no_signature_algorithm() -> None:
    principal = principal_from_identity_card(_card())
    credential = principal.credential_for(CredentialFormat.ED25519_CARD)
    assert credential is not None
    assert credential.algorithm == ""


def test_revoked_agent_identity_maps_to_revoked_principal_status() -> None:
    identity = _jwt_identity(status=AgentIdentityStatus.REVOKED)
    principal = principal_from_agent_identity(identity)
    assert principal.status is PrincipalStatus.REVOKED
    assert principal.is_active is False
    assert principal.has_permission("tasks:read") is False


def test_expired_credential_is_not_valid_for_authority_checks() -> None:
    credential = CredentialRef(
        format=CredentialFormat.JWT,
        ref="jti-0002",
        issued_at=1_000.0,
        expires_at=2_000.0,
    )
    assert credential.is_valid_at(1_500.0) is True
    assert credential.is_valid_at(2_500.0) is False


def test_revoked_credential_is_never_valid() -> None:
    credential = CredentialRef(
        format=CredentialFormat.JWT,
        ref="jti-0003",
        expires_at=0.0,
        revoked=True,
    )
    assert credential.is_valid_at(1.0) is False


def test_merge_refuses_two_principals_with_different_ids() -> None:
    left = principal_from_agent_identity(_jwt_identity())
    right = principal_from_identity_card(_card(agent_id="agent-other"))

    with pytest.raises(ValueError, match="agent-other"):
        left.merge(right)


def test_merge_refuses_principals_from_different_tenants() -> None:
    left = principal_from_agent_identity(_jwt_identity())
    right = AgentPrincipal(id=AGENT_ID, tenant_id="globex")

    with pytest.raises(ValueError, match="tenant"):
        left.merge(right)


def test_merge_keeps_the_more_restrictive_status() -> None:
    active = principal_from_agent_identity(_jwt_identity())
    revoked = AgentPrincipal(id=AGENT_ID, tenant_id="acme", status=PrincipalStatus.REVOKED)

    assert active.merge(revoked).status is PrincipalStatus.REVOKED
    assert revoked.merge(active).status is PrincipalStatus.REVOKED


def test_with_credential_replaces_the_reference_of_the_same_format() -> None:
    principal = principal_from_agent_identity(_jwt_identity())
    rotated = principal.with_credential(
        CredentialRef(format=CredentialFormat.JWT, ref="jti-rotated", algorithm="HS256")
    )

    assert len(rotated.credentials) == 1
    rotated_ref = rotated.credential_for(CredentialFormat.JWT)
    assert rotated_ref is not None
    assert rotated_ref.ref == "jti-rotated"
    original = principal.credential_for(CredentialFormat.JWT)
    assert original is not None
    assert original.ref == "jti-0001"


def test_credential_for_absent_format_returns_none() -> None:
    principal = principal_from_identity_card(_card())
    assert principal.credential_for(CredentialFormat.JWT) is None


def test_principal_round_trips_through_dict() -> None:
    joined = principal_from_agent_identity(_jwt_identity()).merge(principal_from_identity_card(_card()))
    assert AgentPrincipal.from_dict(joined.to_dict()) == joined


def test_principal_rejects_a_blank_id() -> None:
    with pytest.raises(ValueError, match="id"):
        AgentPrincipal(id="  ")


def test_minted_jwt_and_issued_card_resolve_to_one_principal(tmp_path: Path) -> None:
    """The real mint paths, not just the adapters, agree on the agent id.

    The adapter tests above build their source records by hand. This one
    drives the store and the card issuer, so a change to how either mints an
    id fails here rather than being papered over by a fixture that spells the
    id twice.
    """
    store = AgentIdentityStore(tmp_path / "auth")
    identity, _token = store.create_identity(session_id="sess-1", role="backend")
    card = issue_identity_card(identity.id, identity.role, adapter="claude", model="opus")

    from_jwt = principal_from_agent_identity(identity)
    from_card = principal_from_identity_card(card)
    assert from_jwt.id == from_card.id == identity.id

    joined = from_jwt.merge(from_card)
    jwt_credential = joined.credential_for(CredentialFormat.JWT)
    card_credential = joined.credential_for(CredentialFormat.ED25519_CARD)
    assert jwt_credential is not None
    assert card_credential is not None
    assert jwt_credential.ref
    assert card_credential.ref
    assert jwt_credential.ref != card_credential.ref


def test_delegation_receipt_records_a_principal_by_its_id(tmp_path: Path) -> None:
    """A hop recorded for a principal names that principal's id, both ways."""
    store = AgentIdentityStore(tmp_path / "auth")
    identity, _token = store.create_identity(session_id="sess-1", role="backend")
    card = issue_identity_card(identity.id, identity.role, adapter="claude", model="opus")
    subject = principal_from_agent_identity(identity)
    ledger = DelegationLedger(tmp_path / "audit", key=b"test-key")

    receipt = ledger.record_hop(
        run_id="run-1",
        issuer="operator",
        subject=subject,
        audience="task-server",
        act="spawn",
    )

    assert receipt.subject == identity.id
    assert receipt.subject == principal_from_identity_card(card).id
    assert receipt.principal_ids() == ("operator", identity.id)


def test_principal_ref_passes_through_a_non_agent_id() -> None:
    """Parties that hold no credential ("operator", "cli") record as-is."""
    assert principal_ref("operator") == "operator"
    assert principal_ref(AgentPrincipal(id=AGENT_ID)) == AGENT_ID
