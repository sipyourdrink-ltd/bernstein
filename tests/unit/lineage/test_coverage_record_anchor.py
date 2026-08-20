"""Tests for anchoring coverage records into the lineage chain (issue #3770)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.lineage.coverage import (
    COVERAGE_ARTEFACT_KIND,
    anchor_coverage_record,
    find_coverage_for_tool_call,
)
from bernstein.core.lineage.entry import canonicalise
from bernstein.core.lineage.identity import AgentCard, generate_keypair, verify_detached
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.tools.coverage import ToolCoverageRecord, compute_corpus_digest


@pytest.fixture
def hmac_key() -> bytes:
    return b"0" * 64


@pytest.fixture
def card_and_keys() -> tuple[AgentCard, str]:
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker-cov", kid="key-cov-001", public_key_pem=pub_pem)
    return card, priv_pem


@pytest.fixture
def recorder(tmp_path: Path, hmac_key: bytes) -> SignedLineageLog:
    return SignedLineageLog(store=LineageStore(tmp_path / "lineage"), operator_hmac_key=hmac_key)


def _flip_byte(path: Path, offset: int) -> None:
    """XOR byte at ``offset`` with ``0x01``."""
    raw = path.read_bytes()
    mutated = bytearray(raw)
    mutated[offset] ^= 0x01
    path.write_bytes(bytes(mutated))


def test_anchored_coverage_record_tool_call_id_matches_source_call(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    cov = ToolCoverageRecord(
        file_count=4,
        corpus_digest=compute_corpus_digest(["a.py", "b.py"]),
        coverage="complete",
        truncated=False,
        truncation_reason=None,
        exit_status=0,
        exit_checked=True,
    )
    call_id = "tc-search-123"
    entry_h = anchor_coverage_record(
        recorder,
        tool_name="list_dir",
        tool_call_id=call_id,
        coverage=cov,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        span_id="00f067aa0ba902b7",
    )
    assert entry_h.startswith("sha256:")
    entries = list(recorder.store.read_log())
    assert len(entries) == 1
    entry, jws = entries[0]
    assert entry.tool_call_id == call_id
    assert entry.artefact_kind == COVERAGE_ARTEFACT_KIND
    assert entry.artefact_kind == "coverage"
    assert entry.agent_id == card.agent_id
    assert verify_detached(canonicalise(entry), jws, card)


def test_coverage_record_discoverable_by_tool_call_id(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    call_id = "tc-search-456"

    # Record a normal tool result
    recorder.record_write(
        artefact_path="results/output.txt",
        new_content=b"empty result",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id=call_id,
        span_id="span-1",
        artefact_kind="tool-result",
    )

    # Record the corresponding coverage record
    cov = ToolCoverageRecord(
        file_count=0,
        corpus_digest=compute_corpus_digest([]),
        coverage="complete",
        truncated=False,
    )
    anchor_coverage_record(
        recorder,
        tool_name="list_dir",
        tool_call_id=call_id,
        coverage=cov,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        span_id="span-1",
    )

    found = find_coverage_for_tool_call(recorder.store, call_id)
    assert found is not None
    assert found.tool_call_id == call_id
    assert found.artefact_kind == "coverage"


def test_tampered_coverage_record_fails_chain_verify(
    tmp_path: Path,
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    call_id = "tc-tamper-789"
    cov = ToolCoverageRecord(
        file_count=10,
        corpus_digest=compute_corpus_digest(["x.py"]),
        coverage="complete",
    )
    anchor_coverage_record(
        recorder,
        tool_name="list_dir",
        tool_call_id=call_id,
        coverage=cov,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        span_id="span-1",
    )

    log_path = recorder.store.log_path
    raw = log_path.read_bytes()
    assert len(raw) > 20

    # Flip a byte in the log line content
    _flip_byte(log_path, 20)

    # Verifying the detached signature over the modified line fails
    tampered_entries = list(recorder.store.read_log())
    assert len(tampered_entries) == 1
    entry, jws = tampered_entries[0]
    # signature verification must fail because canonical bytes no longer match signed payload
    is_valid = verify_detached(canonicalise(entry), jws, card)
    assert is_valid is False
