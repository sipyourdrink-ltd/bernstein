"""Tests for the ingest adapter plugin contract (issue #4963).

The four proof assertions from issue #4963:

1. A plugin registered out-of-core produces ingestable events end-to-end.
   Register a minimal fake ingest adapter plugin, call
   collect_plugin_ingest_adapters, verify the declaration is registered.

2. An adapter emitting an event shape it did not declare is rejected.
   Call validate_declaration with a declaration missing one of the types
   the adapter can produce, verify ValueError.

3. The receipt names the adapter and version for every ingested event.
   Build an IngestReceipt via IngestOTLPReceipt.ingest_batch with
   adapter_declaration set, verify receipt.to_dict() contains
   adapter_name and adapter_version.

4. Removing the plugin does not break verification of receipts it already
   produced.  Create an IngestReceipt, then verify it round-trips
   from_dict -> to_dict preserving adapter_name and adapter_version
   even when the plugin is absent from the registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.observability.ingest_contract import (
    INGEST_EVENT_TYPES,
    IngestAdapterDeclaration,
)
from bernstein.core.observability.otlp_ingest import OTLPIngestAdapter
from bernstein.core.observability.otlp_ingest_receipt import (
    IngestOTLPReceipt,
    IngestReceipt,
)
from bernstein.plugins import hookimpl
from bernstein.plugins.manager import PluginManager

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def adapter() -> OTLPIngestAdapter:
    return OTLPIngestAdapter()


@pytest.fixture
def audit_chain(tmp_path: Path) -> tuple[Path, bytes]:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    hmac_key = b"x" * 32
    return audit_dir, hmac_key


class _FakeIngestPlugin:
    """Minimal fake ingest adapter plugin."""

    @hookimpl
    def provide_ingest_adapter(self) -> IngestAdapterDeclaration:
        return IngestAdapterDeclaration(
            name="test-fake-adapter",
            version="0.1.0",
            declared_event_types=("gen_ai_activity", "untyped_activity"),
            summary="Fake adapter for contract testing.",
        )


def _genai_span(
    *,
    trace_id: str = "abc123def456abc123def456abc123de",
    span_id: str = "f1e2d3c4b5a69788",
) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "gen_ai.chat",
        "kind": "SPAN_KIND_CLIENT",
        "attributes": [
            {"key": "gen_ai.system", "value": {"stringValue": "anthropic"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-4-6"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "128"}},
            {"key": "gen_ai.usage.completion_tokens", "value": {"intValue": "64"}},
        ],
    }


# --------------------------------------------------------------------------- #
# Proof 1: out-of-core plugin produces ingestable events end-to-end
# --------------------------------------------------------------------------- #


def test_out_of_core_plugin_registered_via_provide_ingest_adapter(
    tmp_path: Path,
) -> None:
    """A plugin that implements provide_ingest_adapter is registered end-to-end.

    A minimal fake plugin is registered with a PluginManager, the manager's
    collect_plugin_ingest_adapters is called, and the resulting declaration
    is retrieved from the registry.
    """
    # Isolate: clear the module-level store before and after so this test is
    # independent of registration order in other tests.
    from bernstein.core.trackers import registry as reg_module

    orig = dict(reg_module._ingest_declarations)
    reg_module._ingest_declarations.clear()
    try:
        pm = PluginManager(workdir=tmp_path)
        pm.register(_FakeIngestPlugin(), name="fake-ingest-plugin")

        added = pm.collect_plugin_ingest_adapters()
        assert added == 1

        decl = reg_module._ingest_declarations.get("test-fake-adapter")
        assert decl is not None
        assert decl.name == "test-fake-adapter"
        assert decl.version == "0.1.0"
        assert "gen_ai_activity" in decl.declared_event_types
        assert "untyped_activity" in decl.declared_event_types
    finally:
        reg_module._ingest_declarations.clear()
        reg_module._ingest_declarations.update(orig)


# --------------------------------------------------------------------------- #
# Proof 2: adapter emitting an event shape it did not declare is rejected
# --------------------------------------------------------------------------- #


def test_validate_declaration_rejects_undeclared_event_type(
    adapter: OTLPIngestAdapter,
) -> None:
    """validate_declaration raises ValueError when the declaration is incomplete.

    The built-in OTLPIngestAdapter declares it can receive both
    'gen_ai_activity' and 'untyped_activity'.  Passing a declaration that
    omits 'untyped_activity' — which the adapter can in fact produce —
    must raise ValueError.
    """
    partial_decl = IngestAdapterDeclaration(
        name="partial-adapter",
        version="1.0.0",
        declared_event_types=("gen_ai_activity",),  # missing untyped_activity
    )
    with pytest.raises(ValueError, match="does not declare"):
        adapter.validate_declaration(partial_decl)


def test_validate_declaration_rejects_completely_empty_decl(
    adapter: OTLPIngestAdapter,
) -> None:
    """A declaration that names no event types raises ValueError."""
    empty_decl = IngestAdapterDeclaration(
        name="empty-adapter",
        version="2.0.0",
        declared_event_types=(),
    )
    with pytest.raises(ValueError, match="does not declare"):
        adapter.validate_declaration(empty_decl)


def test_validate_declaration_accepts_full_decl(
    adapter: OTLPIngestAdapter,
) -> None:
    """A declaration that names all supported event types passes validation."""
    full_decl = IngestAdapterDeclaration(
        name="full-adapter",
        version="1.0.0",
        declared_event_types=("gen_ai_activity", "untyped_activity"),
    )
    # Must not raise
    adapter.validate_declaration(full_decl)


# --------------------------------------------------------------------------- #
# Proof 3: receipt names the adapter and version for every ingested event
# --------------------------------------------------------------------------- #


def test_receipt_contains_adapter_name_and_version(
    audit_chain: tuple[Path, bytes],
    adapter: OTLPIngestAdapter,
) -> None:
    """IngestOTLPReceipt.ingest_batch records adapter_name and adapter_version.

    When adapter_declaration is passed to ingest_batch, the resulting
    IngestReceipt.to_dict() contains the adapter's name and version.
    """
    audit_dir, hmac_key = audit_chain
    mint = IngestOTLPReceipt(
        source_label="test-source",
        profile_name="generic",
        audit_dir=audit_dir,
        hmac_key=hmac_key,
        ingest_adapter=adapter,
    )

    decl = IngestAdapterDeclaration(
        name="my-otlp-adapter",
        version="2.1.0",
        declared_event_types=("gen_ai_activity", "untyped_activity"),
    )

    receipt, _span_results = mint.ingest_batch(
        spans=[_genai_span()],
        adapter_declaration=decl,
    )

    d = receipt.to_dict()
    assert d["adapter_name"] == "my-otlp-adapter"
    assert d["adapter_version"] == "2.1.0"


def test_receipt_adapter_fields_absent_when_no_decl(
    audit_chain: tuple[Path, bytes],
    adapter: OTLPIngestAdapter,
) -> None:
    """When no adapter_declaration is passed, adapter_name and adapter_version
    are empty strings in the receipt."""
    audit_dir, hmac_key = audit_chain
    mint = IngestOTLPReceipt(
        source_label="test-source",
        profile_name="generic",
        audit_dir=audit_dir,
        hmac_key=hmac_key,
        ingest_adapter=adapter,
    )

    receipt, _ = mint.ingest_batch(
        spans=[_genai_span()],
        # adapter_declaration omitted
    )

    d = receipt.to_dict()
    assert d["adapter_name"] == ""
    assert d["adapter_version"] == ""


# --------------------------------------------------------------------------- #
# Proof 4: removing the plugin does not break receipt verification
# --------------------------------------------------------------------------- #


def test_receipt_round_trip_preserves_adapter_fields_without_plugin() -> None:
    """IngestReceipt.from_dict -> to_dict preserves adapter_name and
    adapter_version even when the plugin is no longer in the registry.

    The static-manifest design means the receipt must be self-contained.
    A verifier that never re-imports the plugin must still be able to read
    the adapter name and version from the receipt.
    """
    # Build a receipt with adapter fields set (simulating post-ingest state)
    receipt = IngestReceipt(
        source_label="otel-collector-prod",
        profile_name="generic",
        source_kind="collector",
        coverage="coverage_not_scheduled_by_bernstein",
        coverage_detail="Activity not orchestrated by Bernstein.",
        batch_digest="sha256:abc123",
        span_count=1,
        arrival_index=0,
        adapter_name="my-adapter-v1",
        adapter_version="1.2.3",
    )

    d = receipt.to_dict()
    assert d["adapter_name"] == "my-adapter-v1"
    assert d["adapter_version"] == "1.2.3"

    # Round-trip: reconstruct from dict (simulates a verifier loading from disk)
    rebuilt = IngestReceipt.from_dict(d)
    assert rebuilt.adapter_name == "my-adapter-v1"
    assert rebuilt.adapter_version == "1.2.3"

    # And back to dict again — must be stable
    d2 = rebuilt.to_dict()
    assert d2["adapter_name"] == "my-adapter-v1"
    assert d2["adapter_version"] == "1.2.3"


def test_receipt_from_dict_handles_empty_adapter_fields() -> None:
    """A receipt dict with no adapter fields round-trips cleanly."""
    d = {
        "v": 1,
        "kind": "ingest_receipt",
        "source_label": "test",
        "profile_name": "generic",
        "source_kind": "other",
        "coverage": "coverage_not_scheduled_by_bernstein",
        "coverage_detail": "",
        "batch_digest": "sha256:deadbeef",
        "span_count": 0,
        "arrival_index": 0,
        "claimed_order": [],
        "trace_ids": [],
        "adapter_name": "",
        "adapter_version": "",
        "chain_head": "",
        "timestamp": 0,
        "signer_public_key_pem": "",
        "signature": "",
        "chain_entry_hash": "",
    }
    receipt = IngestReceipt.from_dict(d)
    assert receipt.adapter_name == ""
    assert receipt.adapter_version == ""


def test_ingest_adapter_declaration_all_valid_event_types() -> None:
    """All valid event types together form a valid declaration."""
    adapter = OTLPIngestAdapter()
    decl = IngestAdapterDeclaration(
        name="check",
        version="1.0.0",
        declared_event_types=INGEST_EVENT_TYPES,
    )
    adapter.validate_declaration(decl)
