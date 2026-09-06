"""Adversarial contracts for run-bound, pre-dispatch tool identity."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import pytest

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.lineage.identity import AgentCard, sign_detached
from bernstein.core.security.agent_card_signer import canonicalize_jcs, generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor, IdentitySpawnAnchorError
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_identity import (
    LineageToolCallIdentitySigner,
    ToolCallIdentityAttestation,
    ToolCallIdentityError,
    ToolCallIdentitySignature,
    identity_attestation_ref,
)
from bernstein.core.security.toolcall_interlock import (
    AttestationMode,
    AttestationVerdict,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    ToolCallInterlockError,
    derive_attestation_verdict,
)


def _intent(*, request_id: int = 7) -> ToolCallIntent:
    return ToolCallIntent.from_request(
        scope_id="scope:run-1:agent-1",
        server_name="filesystem",
        method="tools/call",
        tool_name="read_file",
        request_id=request_id,
        span_id=f"span-{request_id}",
        arguments={"path": "/private/value"},
    )


def _provider(tmp_path: Any, *, clock_ns: Any = lambda: 123_000_000_001) -> NativeToolCallEvidenceProvider:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    card_private, card_public = generate_ed25519_keypair()
    tool_private, tool_public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1", role="coder", adapter="codex", model="gpt", created_at=100, expires_at=200
    )
    card_signature = sign_agent_card(card, card_private, kid="spawn-key")
    identity = IdentitySpawnAnchor(chain, {"spawn-key": card_public}, clock=lambda: 150).anchor(
        run_id="run-1",
        card=card,
        signature=card_signature,
        run_journal_head="journal:fixed",
        tool_signing_card=AgentCard(agent_id="agent-1", kid="tool-key", public_key_pem=tool_public.decode()),
    )
    return NativeToolCallEvidenceProvider(
        chain,
        run_identity=identity,
        signer=LineageToolCallIdentitySigner(tool_private.decode(), "tool-key"),
        run_journal_head=lambda: "journal:fixed",
        clock_ns=clock_ns,
    )


def _events(provider: NativeToolCallEvidenceProvider) -> list[dict[str, Any]]:
    return [
        {
            "event_type": event.event_type,
            "resource_id": event.resource_id,
            "details": event.details,
            "hmac": event.hmac,
        }
        for event in provider.chain.query(include_archived=True)
    ]


@pytest.mark.asyncio
async def test_signed_identity_is_frozen_before_dispatch_and_verifies_offline(tmp_path: Any) -> None:
    provider = _provider(tmp_path)
    evidence = await provider.prepare_dispatch(_intent())
    events = _events(provider)
    attestation, dispatch = events[-2:]

    assert attestation["event_type"] == "toolcall.attestation"
    assert dispatch["event_type"] == "toolcall.enforced_dispatch"
    assert attestation["details"]["identity_envelope"]["record"]["attested_at_ns"] == 123_000_000_001
    assert evidence.attestation_ref == dispatch["details"]["attestation_ref"]
    assert derive_attestation_verdict(events) is AttestationVerdict.COMPLETE
    assert "/private/value" not in repr(events)


@pytest.mark.asyncio
async def test_call_indices_are_contiguous_and_deterministic(tmp_path: Any) -> None:
    provider = _provider(tmp_path)
    await provider.prepare_dispatch(_intent(request_id=1))
    await provider.prepare_dispatch(_intent(request_id=2))
    attestations = [event for event in _events(provider) if event["event_type"] == "toolcall.attestation"]
    assert [event["details"]["call_index"] for event in attestations] == [1, 2]


@pytest.mark.asyncio
async def test_mid_run_journal_or_signing_identity_change_requires_new_run(tmp_path: Any) -> None:
    provider = _provider(tmp_path)
    provider.run_journal_head = lambda: "journal:moved"
    with pytest.raises(ToolCallIdentityError, match="new identity-anchored run"):
        await provider.prepare_dispatch(_intent())

    wrong_private, _ = generate_ed25519_keypair()
    provider.run_journal_head = lambda: "journal:fixed"
    provider.signer = LineageToolCallIdentitySigner(wrong_private.decode(), "new-key")
    with pytest.raises(ToolCallIdentityError, match="kid substitution"):
        await provider.prepare_dispatch(_intent())


@pytest.mark.asyncio
async def test_corrupt_or_gapped_existing_history_fails_before_append(tmp_path: Any) -> None:
    provider = _provider(tmp_path)
    await provider.prepare_dispatch(_intent())
    provider._consume_verified_history()
    provider._call_indices[0] = 3
    with pytest.raises(ToolCallIdentityError, match="non-contiguous"):
        await provider.prepare_dispatch(_intent(request_id=8))


@pytest.mark.asyncio
async def test_duplicate_dispatch_and_any_signed_binding_mutation_downgrade(tmp_path: Any) -> None:
    provider = _provider(tmp_path)
    await provider.prepare_dispatch(_intent())
    events = _events(provider)

    duplicate = deepcopy(events)
    duplicate.append(deepcopy(duplicate[-1]))
    assert derive_attestation_verdict(duplicate) is AttestationVerdict.OBSERVED

    for path, value in (
        (("run_id",), "run-other"),
        (("agent_id",), "agent-other"),
        (("args_digest",), "sha256:wrong"),
        (("identity_envelope", "record", "attested_at_ns"), 123_000_000_002),
        (("identity_envelope", "record", "call_index"), 2),
    ):
        mutated = deepcopy(events)
        target = mutated[-2]["details"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert derive_attestation_verdict(mutated) is AttestationVerdict.OBSERVED


@pytest.mark.asyncio
async def test_identity_anchored_run_downgrades_when_an_attestation_lacks_its_envelope(tmp_path: Any) -> None:
    """Absence downgrades: stripping the envelope must never upgrade to complete."""
    provider = _provider(tmp_path)
    await provider.prepare_dispatch(_intent())
    events = _events(provider)
    assert derive_attestation_verdict(events) is AttestationVerdict.COMPLETE

    stripped = deepcopy(events)
    del stripped[-2]["details"]["identity_envelope"]
    assert derive_attestation_verdict(stripped) is AttestationVerdict.OBSERVED


@pytest.mark.asyncio
async def test_unattributable_attestation_downgrades_while_an_identity_anchor_exists(tmp_path: Any) -> None:
    """Emptying run_id is not an escape from the per-run envelope rule."""
    provider = _provider(tmp_path)
    await provider.prepare_dispatch(_intent())
    events = _events(provider)

    unattributable = deepcopy(events)
    del unattributable[-2]["details"]["identity_envelope"]
    unattributable[-2]["details"]["run_id"] = ""
    assert derive_attestation_verdict(unattributable) is AttestationVerdict.OBSERVED


@pytest.mark.asyncio
async def test_legacy_run_without_envelopes_keeps_its_verdict(tmp_path: Any) -> None:
    """A run whose anchor binds no tool key keeps HMAC-only semantics."""
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    private, public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1", role="coder", adapter="codex", model="gpt", created_at=100, expires_at=200
    )
    signature = sign_agent_card(card, private, kid="spawn-key")
    IdentitySpawnAnchor(chain, {"spawn-key": public}, clock=lambda: 150).anchor(
        run_id="run-1", card=card, signature=signature, run_journal_head="journal:fixed"
    )
    provider = NativeToolCallEvidenceProvider(chain)
    await provider.prepare_dispatch(_intent())
    events = _events(provider)
    assert derive_attestation_verdict(events) is AttestationVerdict.COMPLETE


def test_domain_separation_rejects_a_valid_lineage_signature() -> None:
    private, _ = generate_ed25519_keypair()
    record = ToolCallIdentityAttestation(
        1,
        "bernstein.toolcall.identity-attestation",
        "r",
        "a",
        "s",
        "server",
        "tools/call",
        "tool",
        "req",
        "span",
        "sha256:args",
        "sha256:intent",
        1,
        "journal",
        "prev",
        "anchor",
        "kid",
        1,
    )
    ordinary_lineage_signature = sign_detached(record.canonical_bytes(), private.decode(), kid="kid")
    correct_signature = sign_detached(record.signing_bytes(), private.decode(), kid="kid")
    assert ordinary_lineage_signature != correct_signature


@pytest.mark.asyncio
async def test_spawn_anchor_conflict_includes_frozen_tool_key(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    card_private, card_public = generate_ed25519_keypair()
    _, tool_public = generate_ed25519_keypair()
    _, other_public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1", role="coder", adapter="codex", model="gpt", created_at=100, expires_at=200
    )
    signature = sign_agent_card(card, card_private, kid="spawn-key")
    anchor = IdentitySpawnAnchor(chain, {"spawn-key": card_public}, clock=lambda: 150)
    anchor.anchor(
        run_id="run-1",
        card=card,
        signature=signature,
        run_journal_head="journal:fixed",
        tool_signing_card=AgentCard("agent-1", "tool-key", tool_public.decode()),
    )
    with pytest.raises(IdentitySpawnAnchorError, match="conflicting identity"):
        anchor.anchor(
            run_id="run-1",
            card=card,
            signature=signature,
            run_journal_head="journal:fixed",
            tool_signing_card=AgentCard("agent-1", "other-key", other_public.decode()),
        )


@pytest.mark.asyncio
async def test_signer_failure_or_wrong_kid_returns_no_authorizing_handle(tmp_path: Any) -> None:
    provider = _provider(tmp_path)

    class BrokenSigner:
        def sign(self, payload: bytes) -> Any:
            raise RuntimeError("signer unavailable")

    provider.signer = BrokenSigner()
    before = len(provider.chain.query())
    with pytest.raises(RuntimeError, match="unavailable"):
        await provider.prepare_dispatch(_intent())
    assert len(provider.chain.query()) == before

    private, _ = generate_ed25519_keypair()
    provider.signer = LineageToolCallIdentitySigner(private.decode(), "wrong-kid")
    with pytest.raises(ToolCallIdentityError, match="kid substitution"):
        await provider.prepare_dispatch(_intent())
    assert len(provider.chain.query()) == before


@pytest.mark.asyncio
async def test_observed_signer_failure_never_manufactures_enforced_evidence(tmp_path: Any) -> None:
    provider = _provider(tmp_path)

    class BrokenSigner:
        def sign(self, payload: bytes) -> Any:
            raise RuntimeError("signer unavailable")

    provider.signer = BrokenSigner()
    interlock = ToolCallAttestationInterlock(
        provider=provider,
        scope_id="scope:run-1:agent-1",
        mode=AttestationMode.OBSERVED,
    )
    assert await interlock.before_dispatch(_intent()) is None
    assert derive_attestation_verdict(_events(provider)) is AttestationVerdict.OBSERVED


def test_legacy_anchor_is_readable_but_ineligible_for_identity_enforcement(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    private, public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1", role="coder", adapter="codex", model="gpt", created_at=100, expires_at=200
    )
    signature = sign_agent_card(card, private, kid="spawn-key")
    anchor = IdentitySpawnAnchor(chain, {"spawn-key": public}, clock=lambda: 150)
    identity = anchor.anchor(run_id="run-1", card=card, signature=signature, run_journal_head="journal:fixed")
    assert anchor.reconstruct("run-1") == identity
    with pytest.raises(ToolCallIdentityError, match="tool signing identity"):
        NativeToolCallEvidenceProvider(
            chain,
            run_identity=identity,
            signer=LineageToolCallIdentitySigner(private.decode(), "spawn-key"),
            run_journal_head=lambda: "journal:fixed",
        )


@pytest.mark.asyncio
async def test_identity_partial_append_fails_closed(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(tmp_path)
    original = provider.chain.log_with_prev_digest
    calls = 0

    def fail_dispatch(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("audit volume full")
        return original(**kwargs)

    monkeypatch.setattr(provider.chain, "log_with_prev_digest", fail_dispatch)
    interlock = ToolCallAttestationInterlock(provider, "scope:run-1:agent-1")
    with pytest.raises(ToolCallInterlockError):
        await interlock.before_dispatch(_intent())
    assert derive_attestation_verdict(_events(provider)) is AttestationVerdict.OBSERVED


def test_identity_dataclass_exact_shape_is_stable() -> None:
    expected = {
        "v",
        "kind",
        "run_id",
        "agent_id",
        "scope_id",
        "server_name",
        "method",
        "tool_name",
        "request_id",
        "span_id",
        "args_digest",
        "intent_digest",
        "call_index",
        "run_journal_head",
        "prev_chain_digest",
        "identity_anchor_ref",
        "tool_signing_kid",
        "attested_at_ns",
    }
    assert set(ToolCallIdentityAttestation.__dataclass_fields__) == expected


def test_fast_attestation_reference_is_byte_identical_to_full_jcs_envelope() -> None:
    record = ToolCallIdentityAttestation(
        1,
        "bernstein.toolcall.identity-attestation",
        "r",
        "a",
        "s",
        "server",
        "tools/call",
        "tool",
        "req",
        "span",
        "sha256:args",
        "sha256:intent",
        1,
        "journal",
        "prev",
        "anchor",
        "kid",
        1,
    )
    signature = ToolCallIdentitySignature("header..signature", "kid")
    expected = (
        "sha256:"
        + hashlib.sha256(
            canonicalize_jcs({"detached_jws": signature.detached_jws, "record": record.as_dict(), "version": 1})
        ).hexdigest()
    )
    assert (
        identity_attestation_ref(
            record,
            signature,
            record_data=record.as_dict(),
            record_canonical=record.canonical_bytes(),
        )
        == expected
    )


def test_cached_lineage_signer_is_byte_identical_and_does_not_repr_private_key() -> None:
    private, _ = generate_ed25519_keypair()
    payload = b"fixed-domain-separated-payload"
    signer = LineageToolCallIdentitySigner(private.decode(), "kid-1")
    assert signer.sign(payload).detached_jws == sign_detached(payload, private.decode(), kid="kid-1")
    assert private.decode() not in repr(signer)


@pytest.mark.asyncio
async def test_two_warm_provider_instances_reconcile_before_allocating_indices(tmp_path: Any) -> None:
    first = _provider(tmp_path)
    identity = first.run_identity
    signer = first.signer
    assert identity is not None and signer is not None
    second = NativeToolCallEvidenceProvider(
        AuditChainStore(tmp_path / "audit", key=b"k" * 32),
        run_identity=identity,
        signer=signer,
        run_journal_head=lambda: "journal:fixed",
        clock_ns=lambda: 123_000_000_001,
    )

    await first.prepare_dispatch(_intent(request_id=1))
    await second.prepare_dispatch(_intent(request_id=2))
    await first.prepare_dispatch(_intent(request_id=3))

    attestations = [event for event in _events(first) if event["event_type"] == "toolcall.attestation"]
    assert [event["details"]["call_index"] for event in attestations] == [1, 2, 3]
    assert first.chain.verify() == (True, [])


@pytest.mark.asyncio
async def test_native_chain_ignores_a_witness_field_it_did_not_ask_for(tmp_path: Any) -> None:
    """Provenance mode comes from the caller, never from a field in the payload.

    A receipt projection re-chains its range, so anchor identity has to come
    from the retained ``_original_hmac`` witness. That substitution must be
    something a caller opts into. If mere presence of the key selected it, an
    ``identity.spawn_attestation`` whose details carried the key would redirect
    the anchor on a native chain too -- and this function sits on the
    enforcement path, not only the projection path.
    """
    provider = _provider(tmp_path)
    await provider.prepare_dispatch(_intent())
    events = _events(provider)
    assert derive_attestation_verdict(events) is AttestationVerdict.COMPLETE

    anchor = next(event for event in events if event["event_type"] == "identity.spawn_attestation")
    assert anchor["hmac"], "the anchor must carry its own authenticated HMAC"

    planted = deepcopy(events)
    planted_anchor = next(event for event in planted if event["event_type"] == "identity.spawn_attestation")
    planted_anchor["details"]["_original_hmac"] = "0" * 64

    # Native chain: the field is inert, the anchor stays the event's own HMAC.
    assert derive_attestation_verdict(planted) is AttestationVerdict.COMPLETE

    # Witnessed projection: the same bytes now select the anchor, and a witness
    # that does not match what the envelopes were signed against fails closed.
    assert derive_attestation_verdict(planted, witnessed=True) is AttestationVerdict.OBSERVED
