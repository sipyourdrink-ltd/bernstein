"""Contracts for Bernstein's native tool-call evidence provider."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_interlock import (
    AttestationVerdict,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    ToolCallInterlockError,
    derive_attestation_verdict,
)


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


def asdict_event(event: Any) -> dict[str, Any]:
    """Project only the fields consumed by the verdict helper."""
    return {"event_type": event.event_type, "details": event.details}
