"""One agent identity behind both credential formats (issue #5097).

A JWT minted by :mod:`bernstein.core.identity.agent_jwt` and an Ed25519 card
issued by :mod:`bernstein.core.identity.agent_card` used to authenticate two
unrelated types that shared no id space, so nothing could answer "did the same
agent do both of these things". Both now resolve to
:class:`bernstein.core.identity.agent.AgentPrincipal`, and a delegation receipt
names a principal from that same id space.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.identity.agent import (
    AgentPrincipal,
    PrincipalMismatchError,
    merge_principals,
    principal_from_identity_card,
    principal_from_jwt_identity,
)
from bernstein.core.identity.agent_card import issue_identity_card
from bernstein.core.identity.agent_jwt import AgentIdentityStore
from bernstein.core.identity.delegation import DelegationLedger


def _principal_pair(tmp_path: Path) -> tuple[AgentPrincipal, AgentPrincipal, str]:
    """Return ``(jwt_principal, card_principal, agent_id)`` for one agent."""
    store = AgentIdentityStore(tmp_path / "auth")
    identity, _token = store.create_identity(session_id="agent-1", role="backend")
    card = issue_identity_card(identity.id, identity.role, adapter="claude", model="opus")
    return principal_from_jwt_identity(identity), principal_from_identity_card(card), identity.id


def test_jwt_and_ed25519_credentials_resolve_to_one_identity(tmp_path: Path) -> None:
    """Both credential formats produce the same principal type and the same id."""
    jwt_principal, card_principal, agent_id = _principal_pair(tmp_path)

    assert type(jwt_principal) is AgentPrincipal
    assert type(card_principal) is AgentPrincipal
    assert jwt_principal.id == card_principal.id == agent_id

    merged = merge_principals(jwt_principal, card_principal)
    assert merged.id == agent_id
    assert {ref.format for ref in merged.credentials} == {"jwt", "ed25519-card"}
    jwt_ref = merged.credential("jwt")
    card_ref = merged.credential("ed25519-card")
    assert jwt_ref is not None
    assert card_ref is not None
    assert jwt_ref.reference and card_ref.reference
    assert jwt_ref.reference != card_ref.reference


def test_principals_with_different_ids_refuse_to_merge(tmp_path: Path) -> None:
    """Two credentials for different agents are never folded into one principal."""
    jwt_principal, _card_principal, _agent_id = _principal_pair(tmp_path)
    other = principal_from_identity_card(issue_identity_card("agent-2", "backend", adapter="claude", model="opus"))

    with pytest.raises(PrincipalMismatchError):
        merge_principals(jwt_principal, other)


def test_delegation_receipt_references_identity_id(tmp_path: Path) -> None:
    """A hop recorded for a principal names that principal's id, both ways."""
    jwt_principal, card_principal, agent_id = _principal_pair(tmp_path)
    ledger = DelegationLedger(tmp_path / "audit", key=b"test-key")

    receipt = ledger.record_hop(
        run_id="run-1",
        issuer="operator",
        subject=jwt_principal,
        audience="task-server",
        act="spawn",
    )

    assert receipt.subject == agent_id
    assert receipt.subject == card_principal.id
    assert receipt.principal_ids() == ("operator", agent_id)
