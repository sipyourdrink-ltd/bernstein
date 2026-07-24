"""The receipt issuer anchors the authenticated caller in the audit chain (#2609).

The binding directive requires that every accepted task record the
authenticated caller in the audit chain. The receipt issuer already appends
the response to the Merkle-chained lineage spine; passing the caller records
it on that same chain entry, so an auditor can attribute the accepted work to
the identity that called - and two identical calls from the same caller still
project byte-identical receipts (determinism).
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.protocols.a2a.receipt import A2AReceiptIssuer
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter


def _fixed_key(tmp_path: Path) -> Path:
    """Write a fixed-seed Ed25519 key so signatures are byte-stable.

    Determinism of the *projection* is what the AC asserts; a random per-run
    key would make the signature diverge for reasons unrelated to the caller.
    Pinning the key isolates the property under test - two identical calls
    from the same caller and the same node key are byte-identical.
    """
    key_path = tmp_path / "head.pem"
    if not key_path.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return key_path


def _issuer(tmp_path: Path, run_id: str) -> A2AReceiptIssuer:
    return A2AReceiptIssuer(
        spine=LineageSpine(tmp_path / "lineage", run_id=run_id, hmac_key=b"unit-key"),
        kid="test-kid",
        kms_adapter=FileBasedKMSAdapter(_fixed_key(tmp_path.parent / "shared-key"), kid="test-kid"),
    )


def test_caller_is_recorded_on_the_chain_entry(tmp_path: Path) -> None:
    issuer = _issuer(tmp_path, "run-caller")
    issuer.issue(
        task_id="t1",
        response={"message": "m"},
        caller="alice",
        timestamp=1000,
    )
    # The spine entry's actor names the authenticated caller.
    spine = LineageSpine(tmp_path / "lineage", run_id="run-caller", hmac_key=b"unit-key")
    entries = list(spine.iter_entries())
    assert entries
    assert "alice" in entries[-1].actor


def test_same_caller_and_state_yields_byte_identical_receipts(tmp_path: Path) -> None:
    a = _issuer(tmp_path / "a", "r").issue(task_id="t1", response={"message": "m"}, caller="alice", timestamp=1000)
    b = _issuer(tmp_path / "b", "r").issue(task_id="t1", response={"message": "m"}, caller="alice", timestamp=1000)
    assert a.to_dict() == b.to_dict()


def test_different_caller_changes_the_receipt(tmp_path: Path) -> None:
    a = _issuer(tmp_path / "a", "r").issue(task_id="t1", response={"message": "m"}, caller="alice", timestamp=1000)
    b = _issuer(tmp_path / "b", "r").issue(task_id="t1", response={"message": "m"}, caller="bob", timestamp=1000)
    assert a.entry_hash != b.entry_hash


def test_default_caller_keeps_the_historical_actor(tmp_path: Path) -> None:
    """Omitting the caller preserves the pre-existing receipt bytes."""
    issuer = _issuer(tmp_path, "run-default")
    issuer.issue(task_id="t1", response={"message": "m"}, timestamp=1000)
    spine = LineageSpine(tmp_path / "lineage", run_id="run-default", hmac_key=b"unit-key")
    entries = list(spine.iter_entries())
    assert entries[-1].actor == "a2a-server"
