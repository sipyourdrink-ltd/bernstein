"""Unit tests for OTel GenAI span projection of the event journal (#2300).

The span set is a *projection* of the canonical event journal: span ids
are deterministic functions of journal entry hashes, each span carries the
journal entry hash it projects, and the set is signed with the install
identity. Stripping the journal makes the ids unrecomputable; tampering
with a span breaks the entry-hash binding or the signature.

Each test names the acceptance criterion (AC1..AC5) it covers.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.observability.otel_projection import (
    ATTR_JOURNAL_ENTRY_HASH,
    OP_EXECUTE_TOOL,
    OP_INVOKE_AGENT,
    OP_INVOKE_WORKFLOW,
    ProjectionError,
    canonical_projection_bytes,
    derive_span_id,
    derive_trace_id,
    project_spans,
    projection_from_dict,
    projection_to_dict,
    sign_projection,
    to_otlp_spans,
    verify_projection,
)
from bernstein.core.replay.journal import EventJournal, load_events

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_journal(sdd_dir, run_id: str = "run-1") -> EventJournal:
    """Record a representative run into a fresh journal and return it."""
    journal = EventJournal(run_id, sdd_dir)
    journal.record("run_started", goal="ship")
    journal.record("agent_spawned", agent_id="a1")
    journal.record("task_claimed", task_id="t1")
    journal.record("task_completed", task_id="t1")
    journal.record("tick_start", tick=1)  # control-plane: no span
    journal.record("agent_reaped", agent_id="a1")
    journal.record("run_completed", ok=True)
    return journal


def _events(sdd_dir, run_id: str = "run-1") -> list[dict]:
    journal = _write_journal(sdd_dir, run_id)
    return load_events(journal.path)


def _signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b"s" * 32)


# ---------------------------------------------------------------------------
# AC1: two replays export byte-identical trace_id/span_id trees
# ---------------------------------------------------------------------------


def test_two_replays_byte_identical(tmp_path) -> None:
    """AC1: identical journals project byte-identical id trees."""
    events_a = _events(tmp_path / "a")
    events_b = _events(tmp_path / "b")

    proj_a = project_spans(events_a, run_id="run-1")
    proj_b = project_spans(events_b, run_id="run-1")

    assert proj_a.trace_id == proj_b.trace_id
    assert [s.span_id for s in proj_a.spans] == [s.span_id for s in proj_b.spans]
    assert canonical_projection_bytes(proj_a) == canonical_projection_bytes(proj_b)


def test_signature_deterministic_across_replays(tmp_path) -> None:
    """AC1: Ed25519 is deterministic - two signed replays are identical."""
    key = _signing_key()
    sig_a = sign_projection(project_spans(_events(tmp_path / "a"), run_id="run-1"), signing_key=key)
    sig_b = sign_projection(project_spans(_events(tmp_path / "b"), run_id="run-1"), signing_key=key)
    assert sig_a.signature_b64 == sig_b.signature_b64


def test_trace_id_16_bytes_span_id_8_bytes(tmp_path) -> None:
    """AC1: ids have the OTLP-mandated widths (16 / 8 bytes)."""
    proj = project_spans(_events(tmp_path), run_id="run-1")
    assert len(proj.trace_id) == 32
    for span in proj.spans:
        assert len(span.span_id) == 16


def test_span_ids_not_random_are_hash_derived(tmp_path) -> None:
    """AC1: span id equals derive_span_id of the journal entry hash."""
    events = _events(tmp_path)
    proj = project_spans(events, run_id="run-1")
    for span in proj.spans:
        assert span.span_id == derive_span_id(span.entry_hash)
    assert proj.trace_id == derive_trace_id(str(events[0]["event_hash"]))


# ---------------------------------------------------------------------------
# AC2: each span's entry_hash matches an entry in the run journal
# ---------------------------------------------------------------------------


def test_each_span_entry_hash_in_journal(tmp_path) -> None:
    """AC2: every span's bernstein.journal.entry_hash is a real journal row."""
    events = _events(tmp_path)
    journal_hashes = {str(e["event_hash"]) for e in events}
    proj = project_spans(events, run_id="run-1")
    assert proj.spans  # non-empty
    for span in proj.spans:
        anchor = span.attributes[ATTR_JOURNAL_ENTRY_HASH]
        assert anchor in journal_hashes


def test_control_plane_events_do_not_project(tmp_path) -> None:
    """AC2: bookkeeping events (tick_start) are not GenAI spans."""
    events = _events(tmp_path)
    proj = project_spans(events, run_id="run-1")
    operations = [s.operation for s in proj.spans]
    assert OP_INVOKE_WORKFLOW in operations
    assert OP_INVOKE_AGENT in operations
    assert OP_EXECUTE_TOOL in operations
    # tick_start has no mapped operation, so span count < event count.
    assert len(proj.spans) < len(events)


def test_span_tree_parents_are_journal_anchored(tmp_path) -> None:
    """AC2: the root workflow span has no parent; agents parent onto it."""
    proj = project_spans(_events(tmp_path), run_id="run-1")
    roots = [s for s in proj.spans if s.parent_span_id == ""]
    assert roots
    assert roots[0].operation == OP_INVOKE_WORKFLOW
    agents = [s for s in proj.spans if s.operation == OP_INVOKE_AGENT]
    assert agents
    assert agents[0].parent_span_id == roots[0].span_id


# ---------------------------------------------------------------------------
# AC3: verify recomputes span ids and rejects an altered span id
# ---------------------------------------------------------------------------


def test_verify_ok_for_signed_projection(tmp_path) -> None:
    """AC3: a freshly signed projection verifies against the journal + key."""
    key = _signing_key()
    events = _events(tmp_path)
    signed = sign_projection(project_spans(events, run_id="run-1"), signing_key=key)
    result = verify_projection(signed, events, key.public_key())
    assert result.ok, result.errors


def test_verify_rejects_altered_span_id(tmp_path) -> None:
    """AC3: a span whose id was altered is rejected."""
    key = _signing_key()
    events = _events(tmp_path)
    signed = sign_projection(project_spans(events, run_id="run-1"), signing_key=key)
    tampered = projection_to_dict(signed)
    tampered["spans"][1]["span_id"] = "deadbeefdeadbeef"
    result = verify_projection(projection_from_dict(tampered), events, key.public_key())
    assert not result.ok
    assert any("span id mismatch" in e for e in result.errors)


def test_verify_rejects_tampered_entry_hash(tmp_path) -> None:
    """AC3: a span pinned to a hash absent from the journal is rejected."""
    key = _signing_key()
    events = _events(tmp_path)
    signed = sign_projection(project_spans(events, run_id="run-1"), signing_key=key)
    tampered = projection_to_dict(signed)
    tampered["spans"][1]["attributes"][ATTR_JOURNAL_ENTRY_HASH] = "f" * 64
    result = verify_projection(projection_from_dict(tampered), events, key.public_key())
    assert not result.ok
    assert any("not found in journal" in e for e in result.errors)


def test_verify_rejects_wrong_key(tmp_path) -> None:
    """AC3: verifying with a different install identity fails."""
    events = _events(tmp_path)
    signed = sign_projection(project_spans(events, run_id="run-1"), signing_key=_signing_key())
    other = Ed25519PrivateKey.from_private_bytes(b"x" * 32)
    result = verify_projection(signed, events, other.public_key())
    assert not result.ok
    assert any("signature does not verify" in e for e in result.errors)


def test_verify_rejects_unsigned(tmp_path) -> None:
    """AC3: an unsigned projection does not verify."""
    events = _events(tmp_path)
    proj = project_spans(events, run_id="run-1")
    result = verify_projection(proj, events, _signing_key().public_key())
    assert not result.ok
    assert any("unsigned" in e for e in result.errors)


# ---------------------------------------------------------------------------
# AC4: spans validate against OTel GenAI attribute names under the flag
# ---------------------------------------------------------------------------


def test_genai_attributes_present_under_stability_flag(tmp_path) -> None:
    """AC4: with the stability flag on, GenAI convention attrs are emitted."""
    proj = project_spans(_events(tmp_path), run_id="run-1", genai_stability=True)
    for span in proj.spans:
        assert span.attributes["gen_ai.operation.name"] == span.operation
        assert span.attributes["gen_ai.system"] == "bernstein"


def test_genai_attributes_absent_when_flag_off(tmp_path) -> None:
    """AC4: the ids and entry-hash binding never depend on the convention."""
    events = _events(tmp_path)
    proj = project_spans(events, run_id="run-1", genai_stability=False)
    for span in proj.spans:
        assert "gen_ai.operation.name" not in span.attributes
        # journal-anchored binding is still present
        assert ATTR_JOURNAL_ENTRY_HASH in span.attributes
    # ids are identical to the stability-on projection (convention-independent)
    proj_on = project_spans(events, run_id="run-1", genai_stability=True)
    assert [s.span_id for s in proj.spans] == [s.span_id for s in proj_on.spans]


def test_otlp_shape_has_hex_ids_and_attributes(tmp_path) -> None:
    """AC4: OTLP render carries hex trace/span ids and key/value attrs."""
    proj = project_spans(_events(tmp_path), run_id="run-1")
    otlp = to_otlp_spans(proj)
    assert otlp
    first = otlp[0]
    assert first["traceId"] == proj.trace_id
    assert first["spanId"] == proj.spans[0].span_id
    assert any(a["key"] == ATTR_JOURNAL_ENTRY_HASH for a in first["attributes"])


# ---------------------------------------------------------------------------
# AC5: with no OTLP endpoint set, the local projection still emits
# ---------------------------------------------------------------------------


def test_projection_emits_without_any_endpoint(tmp_path, monkeypatch) -> None:
    """AC5: projection is pure - it needs no endpoint, env, or socket."""
    monkeypatch.delenv("BERNSTEIN_OTEL_ENDPOINT", raising=False)
    events = _events(tmp_path)
    proj = project_spans(events, run_id="run-1")
    assert proj.spans
    # a local JSONL store can serialise the OTLP shape with no exporter
    line = json.dumps(to_otlp_spans(proj))
    assert proj.trace_id in line


def test_empty_journal_raises_not_unsigned(tmp_path) -> None:
    """AC5/AC2: no journal means the span set is unproducible."""
    with pytest.raises(ProjectionError):
        project_spans([], run_id="run-1")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_dict(tmp_path) -> None:
    """A signed projection survives a dict round-trip byte-identically."""
    key = _signing_key()
    events = _events(tmp_path)
    signed = sign_projection(project_spans(events, run_id="run-1"), signing_key=key)
    rebuilt = projection_from_dict(projection_to_dict(signed))
    assert rebuilt.signature_b64 == signed.signature_b64
    assert canonical_projection_bytes(rebuilt) == canonical_projection_bytes(signed)
    assert verify_projection(rebuilt, events, key.public_key()).ok
