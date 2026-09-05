"""Adversarial contracts for LineageGate/tool-call evidence coupling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import Task

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.lineage.identity import AgentCard
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.quality.janitor import run_janitor, verify_lineage_tool_call_gate
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_identity import LineageToolCallIdentitySigner
from bernstein.core.security.toolcall_interlock import ToolCallIntent, verified_tool_call_ids

_AUDIT_KEY = b"a" * 32
_LINEAGE_KEY = b"lineage-operator-key"


def _event_dict(event: Any) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "resource_id": event.resource_id,
        "details": event.details,
        "prev_hmac": event.prev_hmac,
        "hmac": event.hmac,
    }


def _seed(
    tmp_path: Path,
    *,
    lineage_tool_call_id: str = "tc-1",
    attested_ids: tuple[str, ...] = ("tc-1",),
) -> AuditChainStore:
    sdd = tmp_path / ".sdd"
    chain = AuditChainStore(sdd / "audit", key=_AUDIT_KEY)
    card_private, card_public = generate_ed25519_keypair()
    tool_private, tool_public = generate_ed25519_keypair()
    identity_card = AgentIdentityCard(
        agent_id="agent-1",
        role="coder",
        adapter="codex",
        model="gpt",
        created_at=100,
        expires_at=200,
    )
    identity = IdentitySpawnAnchor(chain, {"spawn-key": card_public}, clock=lambda: 150).anchor(
        run_id="run-1",
        card=identity_card,
        signature=sign_agent_card(identity_card, card_private, kid="spawn-key"),
        run_journal_head="journal:fixed",
        tool_signing_card=AgentCard("agent-1", "tool-key", tool_public.decode()),
    )
    provider = NativeToolCallEvidenceProvider(
        chain,
        run_identity=identity,
        signer=LineageToolCallIdentitySigner(tool_private.decode(), "tool-key"),
        run_journal_head=lambda: "journal:fixed",
        clock_ns=lambda: 123_000_000_001,
    )
    for call_index, request_id in enumerate(attested_ids, start=1):
        intent = ToolCallIntent.from_request(
            scope_id="scope:run-1:agent-1",
            server_name="filesystem",
            method="tools/call",
            tool_name="write_file",
            request_id=request_id,
            span_id=f"span-{call_index}",
            arguments={"path": "src/result.py", "content": "secret-free"},
        )
        asyncio.run(provider.prepare_dispatch(intent))

    lineage_card = AgentCard("agent-1", "tool-key", tool_public.decode())
    card_dir = sdd / "agents" / lineage_card.agent_id
    card_dir.mkdir(parents=True)
    (card_dir / "card.json").write_text(
        json.dumps(
            {
                "agent_id": lineage_card.agent_id,
                "kid": lineage_card.kid,
                "public_key_pem": lineage_card.public_key_pem,
                "protocolVersion": lineage_card.protocol_version,
            }
        )
    )
    recorder = SignedLineageLog(LineageStore(sdd / "lineage"), operator_hmac_key=_LINEAGE_KEY)
    recorder.record_write(
        artefact_path="src/result.py",
        new_content=b"result = 1\n",
        agent_id=lineage_card.agent_id,
        agent_card=lineage_card,
        private_key_pem=tool_private.decode(),
        tool_call_id=lineage_tool_call_id,
        span_id="span-1",
        ts_ns=123,
    )
    return chain


def test_verified_identity_dispatch_authorizes_matching_lineage_write(tmp_path: Path) -> None:
    chain = _seed(tmp_path)

    result = verify_lineage_tool_call_gate(tmp_path, audit_chain=chain, operator_secret=_LINEAGE_KEY)

    assert result.ok is True, result.failures


def test_missing_or_mismatched_request_id_blocks_the_lineage_write(tmp_path: Path) -> None:
    chain = _seed(tmp_path, lineage_tool_call_id="tc-other")

    result = verify_lineage_tool_call_gate(tmp_path, audit_chain=chain, operator_secret=_LINEAGE_KEY)

    assert result.ok is False
    assert any("tc-other" in failure and "no matching verified" in failure for failure in result.failures)


def test_no_audit_context_preserves_legacy_lineage_behavior(tmp_path: Path) -> None:
    _seed(tmp_path, lineage_tool_call_id="tc-other")

    result = verify_lineage_tool_call_gate(tmp_path, operator_secret=_LINEAGE_KEY)

    assert result.ok is True, result.failures


def test_unsigned_attestation_and_reordered_dispatch_do_not_authorize(tmp_path: Path) -> None:
    chain = _seed(tmp_path)
    events = [_event_dict(event) for event in chain.query(include_archived=True)]
    attestation_index = next(i for i, event in enumerate(events) if event["event_type"] == "toolcall.attestation")
    dispatch_index = next(i for i, event in enumerate(events) if event["event_type"] == "toolcall.enforced_dispatch")

    unsigned = [dict(event) for event in events]
    unsigned[attestation_index] = dict(unsigned[attestation_index])
    unsigned[attestation_index]["details"] = dict(unsigned[attestation_index]["details"])
    unsigned[attestation_index]["details"].pop("identity_envelope")
    assert "tc-1" not in verified_tool_call_ids(unsigned)

    reordered = list(events)
    reordered[attestation_index], reordered[dispatch_index] = reordered[dispatch_index], reordered[attestation_index]
    assert "tc-1" not in verified_tool_call_ids(reordered)

    mismatched = [dict(event) for event in events]
    mismatched[dispatch_index] = dict(mismatched[dispatch_index])
    mismatched[dispatch_index]["details"] = dict(mismatched[dispatch_index]["details"])
    mismatched[dispatch_index]["details"]["intent_digest"] = "sha256:mismatch"
    assert "tc-1" not in verified_tool_call_ids(mismatched)


def test_duplicate_request_id_is_ambiguous_and_blocks(tmp_path: Path) -> None:
    chain = _seed(tmp_path, attested_ids=("tc-1", "tc-1"))

    result = verify_lineage_tool_call_gate(tmp_path, audit_chain=chain, operator_secret=_LINEAGE_KEY)

    assert result.ok is False
    assert any("tc-1" in failure and "no matching verified" in failure for failure in result.failures)


def test_tampered_audit_chain_blocks_before_payload_projection(tmp_path: Path) -> None:
    chain = _seed(tmp_path)
    segment = next((tmp_path / ".sdd" / "audit").glob("*.jsonl"))
    raw = segment.read_text()
    assert '"request_id": "tc-1"' in raw
    segment.write_text(raw.replace('"request_id": "tc-1"', '"request_id": "tc-X"', 1))

    result = verify_lineage_tool_call_gate(tmp_path, audit_chain=chain, operator_secret=_LINEAGE_KEY)

    assert result.ok is False
    assert any("audit chain:" in failure for failure in result.failures)


@pytest.mark.asyncio
async def test_janitor_blocks_merge_on_uncoupled_lineage_write(tmp_path: Path) -> None:
    chain = await asyncio.to_thread(_seed, tmp_path, lineage_tool_call_id="tc-other")
    task = Task(id="T-gate", title="Gate", description="Gate", role="backend")

    results = await run_janitor(
        [task],
        tmp_path,
        lineage_audit_chain=chain,
        lineage_operator_secret=_LINEAGE_KEY,
    )

    assert len(results) == 1
    assert results[0].passed is False
    assert any(not passed and desc == "lineage:tool_call_attestation" for desc, passed, _ in results[0].signal_results)
