"""Re-ingesting an already-anchored span must not append a second chain entry (#4962).

The ingest boundary is fed by transports that retry: a collector that did not
see an acknowledgement replays its batch, and an operator who is unsure whether
a file was consumed runs the command again. If every submission appends, the
chain stops being a record of what the foreign runtime did and becomes a record
of how many times the transport tried, and any count projected over it is wrong.

These tests pin the boundary's identity rule: a chain record written by ingest
is addressed by the content of what was reported, scoped to the identity that
reported it. Re-reporting the same bytes from the same source is a lookup;
reporting different bytes, or the same bytes from a different source, is a new
record.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.observability.otlp_ingest_receipt import IngestOTLPReceipt
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path

SPAN_EVENT = "otlp_ingest_receipt.foreign_span"
ANCHOR_EVENT = "otlp_ingest_receipt.minted"


@pytest.fixture
def audit_chain(tmp_path: Path) -> tuple[Path, bytes]:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir, b"k" * 32


def _mint(audit_chain: tuple[Path, bytes], *, source_label: str = "collector-a") -> IngestOTLPReceipt:
    audit_dir, hmac_key = audit_chain
    return IngestOTLPReceipt(
        source_label=source_label,
        profile_name="generic",
        audit_dir=audit_dir,
        hmac_key=hmac_key,
    )


def _span(*, trace_id: str = "a" * 32, span_id: str = "b" * 16, model: str = "claude-sonnet-4-6") -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "gen_ai.chat",
        "kind": "SPAN_KIND_CLIENT",
        "attributes": {
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat",
        },
    }


def _events(audit_chain: tuple[Path, bytes], event_type: str) -> list[Any]:
    audit_dir, hmac_key = audit_chain
    return AuditChainStore(audit_dir, key=hmac_key).query(event_type=event_type)


def test_reingesting_the_same_span_appends_one_chain_entry_not_two(
    audit_chain: tuple[Path, bytes],
) -> None:
    """A span reported twice by the same source occupies one chain entry."""
    spans = [_span()]

    _mint(audit_chain).ingest_batch(copy.deepcopy(spans))
    _mint(audit_chain).ingest_batch([*copy.deepcopy(spans), _span(trace_id="c" * 32, span_id="d" * 16)])

    span_events = _events(audit_chain, SPAN_EVENT)
    assert len(span_events) == 2, "the repeated span was anchored twice"
    assert len({event.resource_id for event in span_events}) == 2


def test_reingesting_the_same_batch_returns_the_receipt_already_anchored(
    audit_chain: tuple[Path, bytes],
) -> None:
    """A batch whose spans are all already anchored appends nothing and returns its receipt."""
    spans = [_span(), _span(trace_id="c" * 32, span_id="d" * 16)]

    first, _ = _mint(audit_chain).ingest_batch(copy.deepcopy(spans))
    before = len(_events(audit_chain, SPAN_EVENT)) + len(_events(audit_chain, ANCHOR_EVENT))

    second, _ = _mint(audit_chain).ingest_batch(copy.deepcopy(spans))
    after = len(_events(audit_chain, SPAN_EVENT)) + len(_events(audit_chain, ANCHOR_EVENT))

    assert after == before, "a repeated batch grew the chain"
    assert second.binding_digest() == first.binding_digest()
    assert second.signature == first.signature
    assert second.chain_entry_hash == first.chain_entry_hash


def test_a_span_whose_bytes_changed_is_a_new_chain_entry(
    audit_chain: tuple[Path, bytes],
) -> None:
    """Identity is the reported content, not the trace/span id pair."""
    _mint(audit_chain).ingest_batch([_span()])
    _mint(audit_chain).ingest_batch([_span(model="some-other-model")])

    span_events = _events(audit_chain, SPAN_EVENT)
    assert len(span_events) == 2, "a span reporting different bytes was collapsed into the earlier one"


def test_the_same_span_from_a_second_source_is_anchored_separately(
    audit_chain: tuple[Path, bytes],
) -> None:
    """Two sources reporting one span are two attested observations, not one."""
    spans = [_span()]

    _mint(audit_chain, source_label="collector-a").ingest_batch(copy.deepcopy(spans))
    _mint(audit_chain, source_label="collector-b").ingest_batch(copy.deepcopy(spans))

    span_events = _events(audit_chain, SPAN_EVENT)
    assert len(span_events) == 2
    assert len({event.resource_id for event in span_events}) == 2


def test_chain_still_verifies_after_a_duplicate_ingest(
    audit_chain: tuple[Path, bytes],
) -> None:
    """Suppressing a duplicate append must not leave a hole in the chain."""
    audit_dir, hmac_key = audit_chain
    spans = [_span()]

    _mint(audit_chain).ingest_batch(copy.deepcopy(spans))
    _mint(audit_chain).ingest_batch(copy.deepcopy(spans))

    ok, problems = AuditChainStore(audit_dir, key=hmac_key).verify()
    assert ok, problems
