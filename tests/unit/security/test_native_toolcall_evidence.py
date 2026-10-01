"""Contracts for Bernstein's native tool-call evidence provider."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.core.persistence.wal import WALWriter
from bernstein.core.protocols.mcp.mcp_gateway import MCPGateway
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_interlock import (
    AttestationMode,
    AttestationVerdict,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    ToolCallInterlockError,
    canonical_effect_digest,
    derive_attestation_verdict,
    effect_digest_for_connector_response,
    toolcall_effect_outcome,
)

_EFFECT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "toolcall" / "effect_record.json"


def _intent(*, span_id: str = "span-1") -> ToolCallIntent:
    return ToolCallIntent.from_request(
        scope_id="scope:run-1:agent-1",
        server_name="filesystem",
        method="tools/call",
        tool_name="read_file",
        request_id=7,
        span_id=span_id,
        arguments={"path": "/tmp/private-name"},
    )


@pytest.mark.asyncio
async def test_provider_writes_ordered_content_bound_markers(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    provider = NativeToolCallEvidenceProvider(chain)

    evidence = await provider.prepare_dispatch(_intent())
    events = chain.query(resource_id="scope:run-1:agent-1")

    assert [event.event_type for event in events] == [
        "toolcall.attestation",
        "toolcall.enforced_dispatch",
    ]
    assert all(event.details["attestation_ref"] == evidence.attestation_ref for event in events)
    assert all(event.details["intent_digest"] == evidence.intent_digest for event in events)
    assert evidence.dispatch_ref == "hmac:" + events[-1].hmac
    assert derive_attestation_verdict([asdict_event(event) for event in events]) is AttestationVerdict.COMPLETE
    assert chain.verify() == (True, [])

    serialized_details = repr([event.details for event in events])
    assert "/tmp/private-name" not in serialized_details
    assert events[0].details["args_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_partial_append_never_returns_authorizing_evidence(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    provider = NativeToolCallEvidenceProvider(chain)
    interlock = ToolCallAttestationInterlock(provider=provider, scope_id="scope:run-1:agent-1")
    original = chain.log_with_prev_digest
    calls = 0

    def fail_second_append(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("audit volume full")
        return original(**kwargs)

    with (
        patch.object(chain, "log_with_prev_digest", side_effect=fail_second_append),
        pytest.raises(ToolCallInterlockError, match="enforced tool-call attestation preparation failed"),
    ):
        await interlock.before_dispatch(_intent())

    events = chain.query(resource_id="scope:run-1:agent-1")
    assert [event.event_type for event in events] == ["toolcall.attestation"]
    assert derive_attestation_verdict([asdict_event(event) for event in events]) is AttestationVerdict.OBSERVED


@pytest.mark.asyncio
async def test_provider_rejects_empty_intent_fields(tmp_path: Any) -> None:
    provider = NativeToolCallEvidenceProvider(AuditChainStore(tmp_path / "audit", key=b"k" * 32))
    intent = _intent()
    invalid = replace(intent, tool_name="")

    with pytest.raises(ValueError, match="must be non-empty"):
        await provider.prepare_dispatch(invalid)


def _gateway(tmp_path: Any, provider: NativeToolCallEvidenceProvider) -> MCPGateway:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    return MCPGateway(
        upstream_cmd=[],
        wal_writer=WALWriter(run_id="effect-record", sdd_dir=sdd),
        server_name="filesystem",
        attestation_interlock=ToolCallAttestationInterlock(
            provider=provider,
            scope_id="scope:run-1:agent-1",
            mode=AttestationMode.ENFORCED,
        ),
    )


def _tools_call() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "/tmp/private-name"}},
    }


@pytest.mark.asyncio
async def test_effect_record_binds_to_the_intent_digest(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    provider = NativeToolCallEvidenceProvider(chain)
    gateway = _gateway(tmp_path, provider)
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}

    with patch.object(gateway, "_send_request", new_callable=AsyncMock, return_value=(response, 12.5)):
        assert await gateway.handle_jsonrpc(_tools_call()) == response

    events = chain.query(resource_id="scope:run-1:agent-1")
    assert [event.event_type for event in events] == [
        "toolcall.attestation",
        "toolcall.enforced_dispatch",
        "toolcall.effect",
    ]
    attestation, dispatch, effect = events
    assert effect.details["intent_digest"] == attestation.details["intent_digest"]
    assert effect.details["intent_digest"] == dispatch.details["intent_digest"]
    assert effect.details["attestation_ref"] == attestation.details["attestation_ref"]
    assert effect.details["dispatch_ref"] == "hmac:" + dispatch.hmac
    assert effect.details["effect_digest"] == effect_digest_for_connector_response(response)
    assert effect.details["outcome"] == "ok"
    assert effect.details["duration_ms"] == 12.5
    assert chain.verify() == (True, [])


@pytest.mark.asyncio
async def test_missing_effect_record_reads_unobserved_not_success(tmp_path: Any) -> None:
    """Writer produces no effect row. Slice 2 will read that absence as unobserved, never success."""
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    await NativeToolCallEvidenceProvider(chain).prepare_dispatch(_intent())
    events = chain.query(resource_id="scope:run-1:agent-1")
    types = [event.event_type for event in events]
    assert types == ["toolcall.attestation", "toolcall.enforced_dispatch"]
    assert "toolcall.effect" not in types
    assert not any(event.details.get("outcome") == "ok" for event in events)


@pytest.mark.asyncio
async def test_effect_write_failure_does_not_block_connector_result(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    provider = NativeToolCallEvidenceProvider(chain)
    gateway = _gateway(tmp_path, provider)
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    original = chain.log_with_prev_digest

    def fail_effect(**kwargs: Any) -> Any:
        if kwargs.get("event_type") == "toolcall.effect":
            raise OSError("audit volume full")
        return original(**kwargs)

    with (
        patch.object(chain, "log_with_prev_digest", side_effect=fail_effect),
        patch.object(gateway, "_send_request", new_callable=AsyncMock, return_value=(response, 1.0)),
    ):
        assert await gateway.handle_jsonrpc(_tools_call()) == response

    events = chain.query(resource_id="scope:run-1:agent-1")
    assert [event.event_type for event in events] == [
        "toolcall.attestation",
        "toolcall.enforced_dispatch",
    ]


@pytest.mark.asyncio
async def test_timeout_effect_stays_bound_to_the_intent(tmp_path: Any) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    provider = NativeToolCallEvidenceProvider(chain)
    gateway = _gateway(tmp_path, provider)

    with (
        patch.object(gateway, "_send_request", new_callable=AsyncMock, side_effect=TimeoutError),
        pytest.raises(TimeoutError),
    ):
        await gateway.handle_jsonrpc(_tools_call())

    events = chain.query(resource_id="scope:run-1:agent-1")
    assert [event.event_type for event in events] == [
        "toolcall.attestation",
        "toolcall.enforced_dispatch",
        "toolcall.effect",
    ]
    assert events[-1].details["outcome"] == "timeout"
    assert events[-1].details["intent_digest"] == events[0].details["intent_digest"]
    assert events[-1].details["effect_digest"] == effect_digest_for_connector_response(None)


def test_partial_outcome_when_result_and_error_are_both_present() -> None:
    assert (
        toolcall_effect_outcome({"result": {"ok": True}, "error": {"code": -32000, "message": "partial"}}) == "partial"
    )


def test_timestamp_echo_changes_the_raw_effect_digest() -> None:
    base: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    echoed: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"ok": True, "echoed_at": "2026-09-27T10:00:00Z"},
    }
    assert effect_digest_for_connector_response(base) != effect_digest_for_connector_response(echoed)
    assert effect_digest_for_connector_response(base).startswith("sha256:")


def test_patch_effect_digest_matches_result_bundle() -> None:
    import hashlib

    patch = "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n"
    assert canonical_effect_digest(patch) == "sha256:" + hashlib.sha256(patch.encode("utf-8")).hexdigest()


def test_recorded_effect_fixture_digest_is_stable() -> None:
    fixture = json.loads(_EFFECT_FIXTURE.read_text(encoding="utf-8"))
    assert effect_digest_for_connector_response(fixture["response"]) == fixture["effect_digest"]
    assert canonical_effect_digest(fixture["patch"]) == fixture["patch_effect_digest"]


def asdict_event(event: Any) -> dict[str, Any]:
    """Project only the fields consumed by the verdict helper."""
    return {"event_type": event.event_type, "details": event.details}
