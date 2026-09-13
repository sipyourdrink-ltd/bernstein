"""Signed chain-native memory recall receipts (issue #2914)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import bernstein.core.memory.recall_receipt as recall_receipt_module
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.lineage.store import LineageStore
from bernstein.core.memory.chain import MemoryChain, MemoryReplayError, MemoryScope
from bernstein.core.memory.recall_receipt import MemoryRecallReceiptStore
from bernstein.core.security.audit import AuditLog, RetentionPolicy
from bernstein.core.security.audit_chain import EVENT_MEMORY_RECALL, AuditChainStore

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


def test_receipt_file_bytes_match_signed_lineage_content(
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

    stored = store.receipt_path(receipt.receipt_id).read_bytes()
    assert stored == receipt.body_bytes()
    assert "sha256:" + hashlib.sha256(stored).hexdigest() == receipt.receipt_id

    entries = [
        entry
        for entry, _jws in LineageStore(tmp_path / ".sdd" / "lineage").read_log()
        if entry.artefact_path == recall_receipt_module.receipt_artefact_path(receipt.receipt_id)
    ]
    assert len(entries) == 1
    assert entries[0].content_hash == receipt.receipt_id


def test_colon_agent_id_uses_path_safe_identity_card(
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

    identity_root = tmp_path / ".sdd" / "memory" / "recall" / "identity"
    cards = list(identity_root.glob("*/card.json"))
    assert len(cards) == 1
    assert ":" not in cards[0].parent.name
    assert cards[0].parent.name != keypair[0].agent_id
    assert store.verify(receipt.receipt_id).ok


@pytest.mark.parametrize("failure_target", ["receipt", "card"])
def test_preseal_persistence_failure_leaves_no_recall_lineage_or_audit(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    real_write = recall_receipt_module.write_atomic_bytes

    def fail_selected(path: Path, data: bytes, *, mode: int = 0o600) -> None:
        is_receipt = "receipts" in path.parts
        is_card = path.name == "card.json"
        if (failure_target == "receipt" and is_receipt) or (failure_target == "card" and is_card):
            raise OSError("simulated persistence failure")
        real_write(path, data, mode=mode)

    monkeypatch.setattr(recall_receipt_module, "write_atomic_bytes", fail_selected)
    with pytest.raises(OSError, match="simulated persistence failure"):
        store.record_exact(
            scope=MemoryScope.USER,
            namespace="alex",
            query="prefers dark mode",
            run_id="run-recall",
            step_id="recall-1",
            ts_ns=100,
        )

    assert not (tmp_path / ".sdd" / "lineage" / "log.jsonl").exists()
    assert not list((tmp_path / ".sdd" / "audit").glob("*.jsonl"))


def test_audit_failure_retry_reuses_lineage_and_completes_event(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    real_record = recall_receipt_module.record_memory_recall

    def fail_audit(**_kwargs: object) -> object:
        raise OSError("simulated audit append failure")

    monkeypatch.setattr(recall_receipt_module, "record_memory_recall", fail_audit)
    with pytest.raises(OSError, match="simulated audit append failure"):
        store.record_exact(
            scope=MemoryScope.USER,
            namespace="alex",
            query="prefers dark mode",
            run_id="run-recall",
            step_id="recall-1",
            ts_ns=100,
        )

    lineage_store = LineageStore(tmp_path / ".sdd" / "lineage")
    assert len(list(lineage_store.read_log())) == 1

    monkeypatch.setattr(recall_receipt_module, "record_memory_recall", real_record)
    recovered = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=200,
    )
    assert recovered is not None
    assert len(list(lineage_store.read_log())) == 1
    events = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY).query(
        event_type=EVENT_MEMORY_RECALL,
        resource_id=recovered.receipt_id,
        include_archived=True,
    )
    assert len(events) == 1
    assert store.verify(recovered.receipt_id).ok


def test_repeated_identical_recall_reuses_lineage_and_audit(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    store = _store(tmp_path, keypair)
    first = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    second = store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=999,
    )
    assert first is not None and second is not None
    assert first.receipt_id == second.receipt_id
    assert first.lineage_entry_hash == second.lineage_entry_hash
    assert len(list(LineageStore(tmp_path / ".sdd" / "lineage").read_log())) == 1
    events = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY).query(
        event_type=EVENT_MEMORY_RECALL,
        resource_id=first.receipt_id,
        include_archived=True,
    )
    assert len(events) == 1


def test_existing_receipt_bytes_are_not_silently_overwritten(
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
    store.receipt_path(receipt.receipt_id).write_bytes(b"tampered")

    with pytest.raises(MemoryReplayError, match="existing recall receipt bytes"):
        store.record_exact(
            scope=MemoryScope.USER,
            namespace="alex",
            query="prefers dark mode",
            run_id="run-recall",
            step_id="recall-1",
            ts_ns=200,
        )

    assert len(list(LineageStore(tmp_path / ".sdd" / "lineage").read_log())) == 1


def test_existing_lineage_with_different_signer_is_not_reused(tmp_path: Path) -> None:
    _write(tmp_path, claim="prefers dark mode", step_id="s1", timestamp=1)
    private_a, public_a = generate_keypair()
    first_store = MemoryRecallReceiptStore(
        tmp_path,
        agent_card=AgentCard(agent_id="agent:one", kid="key-one", public_key_pem=public_a),
        private_key_pem=private_a,
        operator_hmac_key=_KEY,
    )
    receipt = first_store.record_exact(
        scope=MemoryScope.USER,
        namespace="alex",
        query="prefers dark mode",
        run_id="run-recall",
        step_id="recall-1",
        ts_ns=100,
    )
    assert receipt is not None

    private_b, public_b = generate_keypair()
    second_store = MemoryRecallReceiptStore(
        tmp_path,
        agent_card=AgentCard(agent_id="agent:two", kid="key-two", public_key_pem=public_b),
        private_key_pem=private_b,
        operator_hmac_key=_KEY,
    )
    with pytest.raises(MemoryReplayError, match="current signer"):
        second_store.record_exact(
            scope=MemoryScope.USER,
            namespace="alex",
            query="prefers dark mode",
            run_id="run-recall",
            step_id="recall-1",
            ts_ns=200,
        )
    assert len(list(LineageStore(tmp_path / ".sdd" / "lineage").read_log())) == 1


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


def test_verify_missing_receipt_fails_closed(tmp_path: Path, keypair: tuple[AgentCard, str]) -> None:
    outcome = _store(tmp_path, keypair).verify("sha256:" + "0" * 64)
    assert not outcome.ok
    assert outcome.receipt_id == "sha256:" + "0" * 64
    assert not outcome.checks["receipt_file"]


@pytest.mark.parametrize("payload", [b"{", b"{}", b'{"v":1,"record_hashes":"bad"}'])
def test_verify_malformed_receipt_fails_closed(
    tmp_path: Path,
    keypair: tuple[AgentCard, str],
    payload: bytes,
) -> None:
    store = _store(tmp_path, keypair)
    receipt_id = "sha256:" + "1" * 64
    path = store.receipt_path(receipt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    outcome = store.verify(receipt_id)
    assert not outcome.ok
    assert outcome.receipt_id == receipt_id
    assert not outcome.checks["receipt_file"]


def test_verify_malformed_card_fails_signature_check(
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
    (card_path,) = list((tmp_path / ".sdd" / "memory" / "recall" / "identity").glob("*/card.json"))
    card_path.write_text("{}", encoding="utf-8")

    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert not outcome.checks["signature"]


def test_verify_undecodable_card_fails_signature_check(
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
    (card_path,) = list((tmp_path / ".sdd" / "memory" / "recall" / "identity").glob("*/card.json"))
    card_path.write_bytes(b"\xff")

    outcome = store.verify(receipt.receipt_id)
    assert not outcome.ok
    assert not outcome.checks["signature"]


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
