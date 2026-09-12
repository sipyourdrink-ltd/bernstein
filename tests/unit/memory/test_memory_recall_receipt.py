"""Signed chain-native memory recall receipts (issue #2914)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.memory.chain import MemoryChain, MemoryReplayError, MemoryScope
from bernstein.core.memory.recall_receipt import MemoryRecallReceiptStore
from bernstein.core.security.audit import AuditLog, RetentionPolicy

_KEY = b"k" * 32


@pytest.fixture
def keypair() -> tuple[AgentCard, str]:
    private, public = generate_keypair()
    return AgentCard(agent_id="agent:memory-recall", kid="memory-recall-key", public_key_pem=public), private


def _chain(tmp_path: Path) -> MemoryChain:
    return MemoryChain(tmp_path / ".sdd" / "memory" / "chain", hmac_key=_KEY)


def _anchor(tmp_path: Path, *, step_id: str, timestamp: int) -> str:
    spine = LineageSpine(tmp_path / ".sdd" / "lineage", run_id="run-1", hmac_key=_KEY)
    return spine.record(
        artifact_path=f"memory/{step_id}.txt",
        content=f"anchor:{step_id}".encode(),
        actor="agent:writer",
        step_id=step_id,
        model="test-model",
        timestamp=timestamp,
    )


def _write(tmp_path: Path, *, claim: str, step_id: str, timestamp: int):
    return _chain(tmp_path).write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim=claim,
        actor="agent:writer",
        source_hash=_anchor(tmp_path, step_id=step_id, timestamp=timestamp),
        run_id="run-1",
        step_id=step_id,
        model="test-model",
        timestamp=timestamp,
    )


def _store(tmp_path: Path, keypair: tuple[AgentCard, str]) -> MemoryRecallReceiptStore:
    card, private = keypair
    return MemoryRecallReceiptStore(
        tmp_path,
        agent_card=card,
        private_key_pem=private,
        operator_hmac_key=_KEY,
    )


def test_signed_recall_receipt_records_and_verifies_exact_evidence(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    memory = _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)

    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )

    assert receipt is not None
    assert receipt.record_hashes == (memory.entry_hash,)
    assert receipt.fold_head == memory.entry_hash
    assert receipt.receipt_id == receipt.computed_receipt_id()
    assert receipt.lineage_entry_hash is not None
    assert store.receipt_path(receipt.receipt_id).exists()

    outcome = store.verify(receipt.receipt_id)
    assert outcome.ok, outcome.failures
    assert outcome.checks["signature"]
    assert outcome.checks["operator_hmac"]
    assert outcome.checks["audit_event"]
    assert outcome.checks["recall_replay"]


def test_historical_receipt_survives_later_tombstone_and_replacement(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    original = _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    assert receipt is not None

    chain = _chain(tmp_path)
    chain.forget(
        original.entry_hash,
        scope=MemoryScope.USER,
        namespace="alex",
        actor="agent:writer",
        source_hash=_anchor(tmp_path, step_id="s2", timestamp=2),
        run_id="run-1",
        step_id="s2",
        model="test-model",
        timestamp=2,
    )
    replacement = _write(tmp_path, claim="prefers dark mode", step_id="s3", timestamp=3)

    current = chain.recall_exact("prefers dark mode", scope=MemoryScope.USER, namespace="alex")
    assert current.record_hashes == (replacement.entry_hash,)
    assert receipt.record_hashes == (original.entry_hash,)

    outcome = store.verify(receipt.receipt_id)
    assert outcome.ok, outcome.failures
    assert outcome.checks["recall_replay"]


def test_recall_receipt_verifies_after_audit_segment_is_archived(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    assert receipt is not None
    assert store.verify(receipt.receipt_id).ok

    audit_dir = tmp_path / ".sdd" / "audit"
    archived = AuditLog(audit_dir=audit_dir, key=_KEY).archive(RetentionPolicy(retention_days=-1))
    assert archived.archived
    assert not list(audit_dir.glob("*.jsonl"))

    outcome = store.verify(receipt.receipt_id)
    assert outcome.ok, outcome.failures
    assert outcome.checks["audit_chain"]
    assert outcome.checks["audit_event"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query", "prefers light mode"),
        ("fold_hash", "sha256:" + "0" * 64),
        ("record_hashes", ["sha256:" + "1" * 64]),
    ],
)
def test_receipt_body_tamper_fails_verification(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
    field: str,
    value: object,
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    assert receipt is not None

    path = store.receipt_path(receipt.receipt_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert not outcome.checks["receipt_body"] or not outcome.checks.get("recall_replay", False)


@pytest.mark.parametrize("mode", ["corrupt", "remove"])
def test_missing_or_tampered_jws_fails_verification(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
    mode: str,
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    assert receipt is not None

    signatures = list((tmp_path / ".sdd" / "lineage" / "signatures").rglob("*.jws"))
    assert len(signatures) == 1
    if mode == "remove":
        signatures[0].unlink()
    else:
        signatures[0].write_text("invalid-jws", encoding="utf-8")

    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert not outcome.checks["signature"]


@pytest.mark.parametrize("mode", ["edit", "remove"])
def test_referenced_memory_chain_prefix_tamper_fails_verification(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
    mode: str,
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    assert receipt is not None

    path = _chain(tmp_path).chain_path(MemoryScope.USER, "alex")
    if mode == "remove":
        path.write_text("", encoding="utf-8")
    else:
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("prefers dark mode", "prefers light mode"), encoding="utf-8")

    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert not outcome.checks["memory_chain"] or not outcome.checks["recall_replay"]


def test_empty_namespace_recall_is_noop_with_no_receipt_writes(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    store = _store(tmp_path, keypair)

    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="missing",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )

    assert receipt is None
    assert not (tmp_path / ".sdd" / "memory" / "recall").exists()
    assert not (tmp_path / ".sdd" / "lineage" / "log.jsonl").exists()
    assert not (tmp_path / ".sdd" / "audit").exists()


def test_no_match_recall_is_noop_with_no_receipt_writes(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)

    receipt = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="missing",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )

    assert receipt is None
    assert not (tmp_path / ".sdd" / "memory" / "recall").exists()
    assert not (tmp_path / ".sdd" / "lineage" / "log.jsonl").exists()
    assert not (tmp_path / ".sdd" / "audit").exists()


def test_unknown_fold_head_fails_closed_before_any_receipt_write(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)

    with pytest.raises(MemoryReplayError, match="fold head"):
        store.record_exact(
            scope=MemoryScope.USER,
            namespace="alex",
            query="prefers dark mode",
            fold_head="sha256:" + "f" * 64,
            run_id="run-recall",
            step_id="recall-1",
            ts_ns=100,
        )

    assert not (tmp_path / ".sdd" / "memory" / "recall").exists()
    assert not (tmp_path / ".sdd" / "lineage" / "log.jsonl").exists()
    assert not (tmp_path / ".sdd" / "audit").exists()
