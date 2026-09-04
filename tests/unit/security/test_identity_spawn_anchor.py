from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor, IdentitySpawnAnchorError


def _cross_process_anchor(audit_dir, public_key, card, signature, ready, start, results):
    anchor = IdentitySpawnAnchor(
        AuditChainStore(audit_dir, key=b"k" * 32),
        {signature.kid: public_key},
        clock=lambda: 150,
    )
    ready.put(card.agent_id)
    if not start.wait(timeout=10):
        results.put(("timeout", card.agent_id))
        return
    try:
        anchor.anchor(
            run_id="run-1",
            card=card,
            signature=signature,
            run_journal_head="journal:1",
        )
    except IdentitySpawnAnchorError as exc:
        results.put(("rejected", str(exc)))
    else:
        results.put(("anchored", card.agent_id))


def setup_anchor(tmp_path):
    private, public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1", role="coder", adapter="codex", model="gpt", created_at=100, expires_at=200
    )
    signature = sign_agent_card(card, private, kid="agent-bernstein-orchestrator")
    anchor = IdentitySpawnAnchor(
        AuditChainStore(tmp_path / "audit", key=b"k" * 32), {signature.kid: public}, clock=lambda: 150
    )
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
        anchor.anchor(
            run_id="run-1", card=replace(card, agent_id="attacker"), signature=signature, run_journal_head="journal:1"
        )


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


def test_retry_with_moved_journal_head_is_rejected_and_surfaced(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    with pytest.raises(IdentitySpawnAnchorError, match="journal head moved"):
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


def test_historical_anchor_survives_later_card_expiry(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    expected = anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    anchor.clock = lambda: 999
    assert anchor.reconstruct("run-1") == expected


def test_validation_timestamp_is_recorded_and_checked_historically(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    event = anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")[0]
    assert event.details["validated_at"] == 150

    event.details["validated_at"] = 250
    with (
        patch.object(anchor.chain, "query", return_value=[event]),
        pytest.raises(IdentitySpawnAnchorError, match="recorded validation time"),
    ):
        anchor.reconstruct("run-1")


def test_live_key_rotation_does_not_orphan_historical_verification(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    expected = anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    anchor.trusted_public_keys = {}
    assert anchor.reconstruct("run-1") == expected


def test_missing_frozen_historical_public_key_fails_closed(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    event = anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")[0]
    event.details.pop("verification_key_jwk")
    with (
        patch.object(anchor.chain, "query", return_value=[event]),
        pytest.raises(IdentitySpawnAnchorError, match="frozen historical verification key"),
    ):
        anchor.reconstruct("run-1")


def test_frozen_historical_public_key_tamper_fails_digest_check(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    event = anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")[0]
    event.details["verification_key_jwk"]["kid"] = "attacker"
    with (
        patch.object(anchor.chain, "query", return_value=[event]),
        pytest.raises(IdentitySpawnAnchorError, match="verification key digest mismatch"),
    ):
        anchor.reconstruct("run-1")


def test_self_asserted_key_cannot_replace_trust_source(tmp_path):
    anchor, card, _signature, _ = setup_anchor(tmp_path)
    attacker_private, attacker_public = generate_ed25519_keypair()
    forged_card = replace(card, extensions={"public_key": attacker_public.decode()})
    forged_signature = sign_agent_card(forged_card, attacker_private, kid="agent-bernstein-orchestrator")
    with pytest.raises(IdentitySpawnAnchorError, match="not trusted"):
        anchor.anchor(run_id="run-1", card=forged_card, signature=forged_signature, run_journal_head="journal:1")


def test_competing_thread_identities_cannot_both_anchor(tmp_path):
    anchor, card, signature, private = setup_anchor(tmp_path)
    other = replace(card, agent_id="agent-2")
    other_signature = sign_agent_card(other, private, kid="agent-bernstein-orchestrator")

    def attempt(candidate, signed):
        try:
            return anchor.anchor(run_id="run-1", card=candidate, signature=signed, run_journal_head="journal:1")
        except IdentitySpawnAnchorError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: attempt(*pair), [(card, signature), (other, other_signature)]))
    assert sum(result is not None for result in results) == 1
    assert len(anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")) == 1


def test_competing_cross_process_identities_cannot_both_anchor(tmp_path):
    _anchor, card, signature, private = setup_anchor(tmp_path)
    other = replace(card, agent_id="agent-2")
    other_signature = sign_agent_card(other, private, kid="agent-bernstein-orchestrator")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_cross_process_anchor,
            args=(
                tmp_path / "audit",
                _anchor.trusted_public_keys[signature.kid],
                candidate,
                signed,
                ready,
                start,
                results,
            ),
        )
        for candidate, signed in ((card, signature), (other, other_signature))
    ]

    for process in processes:
        process.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"agent-1", "agent-2"}
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=10), results.get(timeout=10)]
    assert [status for status, _detail in outcomes].count("anchored") == 1
    assert [status for status, _detail in outcomes].count("rejected") == 1
    assert "conflicting identity" in next(detail for status, detail in outcomes if status == "rejected")

    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    assert chain.verify() == (True, [])
    assert len(chain.query(event_type="identity.spawn_attestation", resource_id="run-1")) == 1


def test_separate_store_append_cannot_move_identity_predecessor(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    other_store = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    other_store.log(event_type="unrelated", actor="other", resource_type="test", resource_id="other")
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    assert anchor.chain.verify() == (True, [])
    events = anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")
    assert events[0].details["prev_chain_digest"] != ""


def test_physical_chain_tamper_blocks_reconstruction(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    log_path = next((tmp_path / "audit").glob("*.jsonl"))
    original = log_path.read_text()
    log_path.write_text(original.replace("agent-1", "agent-X", 1))
    with pytest.raises(IdentitySpawnAnchorError, match="audit chain verification failed"):
        anchor.reconstruct("run-1")


def test_signed_envelope_corruption_fails_digest_check(tmp_path):
    anchor, card, signature, _ = setup_anchor(tmp_path)
    anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:1")
    event = anchor.chain.query(event_type="identity.spawn_attestation", resource_id="run-1")[0]
    event.details["signed_card"]["card"]["agent_id"] = "agent-X"
    with (
        patch.object(anchor.chain, "query", return_value=[event]),
        pytest.raises(IdentitySpawnAnchorError, match="digest mismatch"),
    ):
        anchor.reconstruct("run-1")
