"""Negative contracts for enforced versus observed tool-call attestation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.core.persistence.wal import WALWriter
from bernstein.core.protocols.mcp.mcp_gateway import MCPGateway
from bernstein.core.security.toolcall_interlock import (
    AttestationMode,
    AttestationVerdict,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    ToolCallInterlockError,
    VerifiedDispatchEvidence,
    derive_attestation_verdict,
    project_attestation_mode,
)


class _FailingProvider:
    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        del intent
        raise OSError("audit volume full")


class _RecordingProvider:
    def __init__(self, *, stale: bool = False) -> None:
        self.intents: list[ToolCallIntent] = []
        self.stale = stale

    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        self.intents.append(intent)
        return VerifiedDispatchEvidence(
            attestation_ref=f"attestation:{intent.span_id}",
            dispatch_ref=f"dispatch:{intent.span_id}",
            intent_digest="sha256:stale" if self.stale else intent.digest(),
        )


def _gateway(tmp_path: Any, mode: AttestationMode) -> MCPGateway:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    writer = WALWriter(run_id=f"attestation-{mode}", sdd_dir=sdd)
    return MCPGateway(
        upstream_cmd=[],
        wal_writer=writer,
        server_name="filesystem",
        attestation_interlock=ToolCallAttestationInterlock(
            provider=_FailingProvider(), scope_id="scope:test-run:agent-1", mode=mode
        ),
    )


def _request() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "/tmp/x"}},
    }


@pytest.mark.asyncio
async def test_enforced_failure_makes_connector_unreachable(tmp_path: Any) -> None:
    gateway = _gateway(tmp_path, AttestationMode.ENFORCED)

    with (
        patch.object(gateway, "_send_request", new_callable=AsyncMock) as connector,
        pytest.raises(ToolCallInterlockError, match="enforced tool-call attestation preparation failed"),
    ):
        await gateway.handle_jsonrpc(_request())

    connector.assert_not_awaited()


@pytest.mark.asyncio
async def test_observed_failure_allows_call_and_receipt_stays_observed(tmp_path: Any) -> None:
    gateway = _gateway(tmp_path, AttestationMode.OBSERVED)
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}

    with patch.object(gateway, "_send_request", new_callable=AsyncMock, return_value=(response, 0.0)) as send:
        result = await gateway.handle_jsonrpc(_request())

    assert result == response
    send.assert_awaited_once()
    # Receipt construction projects an empty marker range rather than failing;
    # the absence of enforcement evidence is the observed verdict.
    receipt = project_attestation_mode([], claimed_mode="complete")
    assert receipt.verdict is AttestationVerdict.OBSERVED
    assert not receipt.complete


@pytest.mark.asyncio
async def test_matching_evidence_reaches_connector_with_content_bound_intent(tmp_path: Any) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    provider = _RecordingProvider()
    gateway = MCPGateway(
        upstream_cmd=[],
        wal_writer=WALWriter(run_id="attestation-matched", sdd_dir=sdd),
        server_name="filesystem",
        attestation_interlock=ToolCallAttestationInterlock(
            provider=provider,
            scope_id="scope:test-run:agent-1",
            mode=AttestationMode.ENFORCED,
        ),
    )
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}

    with patch.object(gateway, "_send_request", new_callable=AsyncMock, return_value=(response, 0.0)) as send:
        assert await gateway.handle_jsonrpc(_request()) == response

    send.assert_awaited_once()
    assert len(provider.intents) == 1
    intent = provider.intents[0]
    assert intent.scope_id == "scope:test-run:agent-1"
    assert intent.server_name == "filesystem"
    assert intent.tool_name == "read_file"
    assert intent.args_digest.startswith("sha256:")
    assert intent.span_id


@pytest.mark.asyncio
async def test_stale_evidence_for_different_intent_blocks_connector(tmp_path: Any) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    gateway = MCPGateway(
        upstream_cmd=[],
        wal_writer=WALWriter(run_id="attestation-stale", sdd_dir=sdd),
        server_name="filesystem",
        attestation_interlock=ToolCallAttestationInterlock(
            provider=_RecordingProvider(stale=True),
            scope_id="scope:test-run:agent-1",
            mode=AttestationMode.ENFORCED,
        ),
    )
    with (
        patch.object(gateway, "_send_request", new_callable=AsyncMock) as connector,
        pytest.raises(ToolCallInterlockError, match="different tool-call intent"),
    ):
        await gateway.handle_jsonrpc(_request())

    connector.assert_not_awaited()


def test_complete_claim_without_chain_markers_is_downgraded() -> None:
    receipt_claim = {"claimed_mode": "complete"}

    projection = project_attestation_mode([receipt_claim], claimed_mode="complete")

    assert projection.claimed_mode == "complete"
    assert projection.verdict is AttestationVerdict.OBSERVED


def test_complete_is_derived_only_from_ordered_matching_markers() -> None:
    events: list[Mapping[str, Any]] = [
        {
            "event_type": "toolcall.attestation",
            "details": {"attestation_ref": "sha256:a", "intent_digest": "sha256:intent"},
        },
        {
            "event_type": "toolcall.enforced_dispatch",
            "details": {"attestation_ref": "sha256:a", "intent_digest": "sha256:intent"},
        },
    ]

    assert derive_attestation_verdict(events) is AttestationVerdict.COMPLETE
    assert derive_attestation_verdict(list(reversed(events))) is AttestationVerdict.OBSERVED
    mismatched: list[Mapping[str, Any]] = [
        events[0],
        {
            "event_type": "toolcall.enforced_dispatch",
            "details": {"attestation_ref": "sha256:a", "intent_digest": "sha256:different"},
        },
    ]
    assert derive_attestation_verdict(mismatched) is AttestationVerdict.OBSERVED
