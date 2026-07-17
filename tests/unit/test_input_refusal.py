"""Signed input-refusal receipts (#2545, AC2).

A malformed input is refused with a signed, chain-anchored receipt that carries
the JSONPath of the offending field, verifies offline against the chain, and
flips to fail when either the receipt bytes or its chain entry are tampered.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.security.audit_chain import EVENT_INPUT_REFUSAL, AuditChainStore
from bernstein.core.security.input_refusal import (
    BOUNDARY_SCHEDULE_FIRE,
    InputRefusalReceipt,
    build_refusal_receipt,
    read_refusal_receipt,
    refuse_input,
    verify_refusal_against_chain,
    verify_refusal_receipt,
    write_refusal_receipt,
)
from bernstein.core.tasks.param_contract import ParamContract, ParamContractViolation


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _keys() -> tuple[str, str]:
    return generate_keypair()


def _receipt(priv: str, pub: str, **over: object) -> InputRefusalReceipt:
    base = dict(
        boundary=BOUNDARY_SCHEDULE_FIRE,
        resource_id="sched_1",
        json_path="$.params.target",
        schema_hash="sha256:" + "a" * 64,
        value_digest="sha256:" + "b" * 64,
        reason_code="bad_type",
        message="target: expected int",
        private_key_pem=priv,
        public_key_pem=pub,
        refused_at=1_700_000_000,
    )
    base.update(over)
    return build_refusal_receipt(**base)  # type: ignore[arg-type]


def test_receipt_signature_verifies() -> None:
    priv, pub = _keys()
    receipt = _receipt(priv, pub)
    assert verify_refusal_receipt(receipt) is True
    assert receipt.json_path == "$.params.target"
    assert receipt.receipt_hash().startswith("sha256:")


def test_receipt_hash_is_operator_independent() -> None:
    # Two operators (distinct keys, distinct clocks) refusing the same input
    # against the same schema derive the same content hash.
    p1, u1 = _keys()
    p2, u2 = _keys()
    r1 = _receipt(p1, u1, refused_at=1)
    r2 = _receipt(p2, u2, refused_at=999)
    assert r1.receipt_hash() == r2.receipt_hash()


def test_refuse_input_anchors_and_verifies_offline(tmp_path: Path) -> None:
    priv, pub = _keys()
    chain = _chain(tmp_path)
    receipt = refuse_input(
        chain=chain,
        sdd_dir=tmp_path / ".sdd",
        boundary=BOUNDARY_SCHEDULE_FIRE,
        resource_id="sched_1",
        json_path="$.params.target",
        schema_hash="sha256:" + "a" * 64,
        value_digest="sha256:" + "b" * 64,
        reason_code="bad_type",
        message="target: expected int",
        private_key_pem=priv,
        public_key_pem=pub,
    )
    # Exactly one refusal event anchored.
    events = chain.query(event_type=EVENT_INPUT_REFUSAL)
    assert len(events) == 1
    assert events[0].details["json_path"] == "$.params.target"
    assert events[0].details["receipt_hash"] == receipt.receipt_hash()

    result = verify_refusal_against_chain(chain, receipt)
    assert result.ok, result.reason
    assert result.signature_ok and result.chain_ok and result.anchored


def test_tampering_receipt_bytes_flips_verification(tmp_path: Path) -> None:
    priv, pub = _keys()
    chain = _chain(tmp_path)
    receipt = refuse_input(
        chain=chain,
        sdd_dir=None,
        boundary=BOUNDARY_SCHEDULE_FIRE,
        resource_id="sched_1",
        json_path="$.params.target",
        schema_hash="sha256:" + "a" * 64,
        value_digest="sha256:" + "b" * 64,
        reason_code="bad_type",
        message="target: expected int",
        private_key_pem=priv,
        public_key_pem=pub,
    )
    # Mutate the JSONPath -> signature no longer covers the body AND the
    # recomputed content hash no longer matches the anchored entry.
    tampered = replace(receipt, json_path="$.params.other")
    assert verify_refusal_receipt(tampered) is False
    result = verify_refusal_against_chain(chain, tampered)
    assert result.ok is False
    assert "signature" in result.reason or "not anchored" in result.reason


def test_tampering_chain_entry_flips_verification(tmp_path: Path) -> None:
    priv, pub = _keys()
    chain = _chain(tmp_path)
    receipt = refuse_input(
        chain=chain,
        sdd_dir=None,
        boundary=BOUNDARY_SCHEDULE_FIRE,
        resource_id="sched_1",
        json_path="$.params.target",
        schema_hash="sha256:" + "a" * 64,
        value_digest="sha256:" + "b" * 64,
        reason_code="bad_type",
        message="target: expected int",
        private_key_pem=priv,
        public_key_pem=pub,
    )
    # Corrupt the on-disk audit log line so chain.verify() fails.
    log_files = list((tmp_path / "audit").glob("*.jsonl"))
    assert log_files
    content = log_files[0].read_text().replace("schedule.fire", "recipe.launch")
    log_files[0].write_text(content)
    fresh = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    result = verify_refusal_against_chain(fresh, receipt)
    assert result.ok is False
    assert "chain" in result.reason.lower() or "not anchored" in result.reason


def test_receipt_persists_and_round_trips(tmp_path: Path) -> None:
    priv, pub = _keys()
    receipt = _receipt(priv, pub)
    path = write_refusal_receipt(tmp_path / ".sdd", receipt)
    loaded = read_refusal_receipt(path)
    assert loaded is not None
    assert loaded.receipt_hash() == receipt.receipt_hash()
    assert verify_refusal_receipt(loaded) is True


def test_wrong_key_does_not_verify() -> None:
    priv, pub = _keys()
    _, other_pub = _keys()
    receipt = _receipt(priv, pub)
    forged = replace(receipt, signer_public_key_pem=other_pub)
    assert verify_refusal_receipt(forged) is False


def test_receipt_from_param_contract_violation(tmp_path: Path) -> None:
    # End-to-end: a real contract violation feeds a refusal receipt whose
    # JSONPath / schema hash / value digest come straight from the violation.
    priv, pub = _keys()
    chain = _chain(tmp_path)
    contract = ParamContract.from_schema([{"name": "retries", "type": "int", "required": True}])
    with pytest.raises(ParamContractViolation) as exc:
        contract.validate_and_coerce({"retries": "not-an-int"})
    v = exc.value
    receipt = refuse_input(
        chain=chain,
        sdd_dir=None,
        boundary=BOUNDARY_SCHEDULE_FIRE,
        resource_id="sched_x",
        json_path=v.json_path,
        schema_hash=v.schema_hash,
        value_digest=v.value_digest,
        reason_code=v.reason_code,
        message=str(v),
        private_key_pem=priv,
        public_key_pem=pub,
    )
    assert receipt.json_path == "$.params.retries"
    assert verify_refusal_against_chain(chain, receipt).ok
