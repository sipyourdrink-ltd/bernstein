from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from bernstein.core.security.agent_card_signer import generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.agent_identity import AgentIdentityCard
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor, IdentitySpawnAnchorError


def setup_anchor(tmp_path):
    private, public = generate_ed25519_keypair()
    card = AgentIdentityCard(agent_id="agent-1", role="coder", adapter="codex", model="gpt", created_at=100, expires_at=200)
    signature = sign_agent_card(card, private, kid="agent-bernstein-orchestrator")
    anchor = IdentitySpawnAnchor(AuditChainStore(tmp_path / "audit", key=b"k" * 32), {signature.kid: public}, clock=lambda: 150)
    return anchor, card, signature, private


def test_valid_card_anchors_once_and_reconstructs(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    expected = anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    assert anchor.reconstruct("run-1") == expected


def test_unsigned_or_untrusted_card_is_rejected(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.trusted_public_keys = {}
    with pytest.raises(IdentitySpawnAnchorError, match="not trusted"):
        anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")


def test_wrong_key_is_rejected(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    _, wrong_public = generate_ed25519_keypair()
    anchor.trusted_public_keys = {signature.kid: wrong_public}
    with pytest.raises(IdentitySpawnAnchorError, match="not trusted"):
        anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")


def test_post_signing_mutation_is_rejected(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    with pytest.raises(IdentitySpawnAnchorError, match="not trusted"):
        anchor.anchor(run_id="run-1", card=replace(card, agent_id="attacker"), signature=signature, run_journal_head="journal:1")


def test_kid_substitution_is_rejected(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    forged = replace(signature, kid="other")
    with pytest.raises(IdentitySpawnAnchorError, match="substitution"):
        anchor.anchor(run_id="run-1", card=card, signature=forged, run_journal_head="journal:1")


@pytest.mark.parametrize("created,expires", [(151, 200), (100, 150)])
def test_invalid_validity_window_is_rejected(tmp_path, created, expires):
    anchor, card, _signature, private = setup_anchor(tmp_path)
    card = replace(card, created_at=created, expires_at=expires)
    signature = sign_agent_card(card, private, kid="agent-bernstein-orchestrator")
    with pytest.raises(IdentitySpawnAnchorError, match="not valid"):
        anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")


def test_identical_retry_is_idempotent(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    first = anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    assert anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1") == first
    assert len(anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")) == 1


def test_conflicting_retry_is_rejected(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    with pytest.raises(IdentitySpawnAnchorError, match="conflicting"):
        anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:2")


def test_empty_svid_and_genesis_are_honest(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    identity = anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="")
    assert identity.svid_reference == ""
    assert identity.run_journal_head == ""


def test_missing_signed_card_evidence_fails_closed(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    event = anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")[0]
    event.details.pop("signed_card")
    with (
        patch.object(anchor.chain, "query", return_value=[event]),
        pytest.raises(IdentitySpawnAnchorError, match="unavailable"),
    ):
        anchor.reconstruct("run-1")
