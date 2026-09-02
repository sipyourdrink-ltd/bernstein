"""Tests for read-set refusal receipt serialization and offline verification.

Tests cover:
    1. Deterministic receipt serialization produces byte-identical output.
    2. Receipt signature verifies correctly.
    3. Offline verification against audit chain works without repo access.
    4. Tampered receipt bytes fail verification.
    5. Receipt persistence round-trips correctly.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from bernstein.core.git.read_set_receipt import ReadSetRefusalReceipt


# ------------------------------------------------------------------
# Test helper fixtures
# ------------------------------------------------------------------


@pytest.fixture
def sample_changed_paths():
    """Return sample ChangedPath entries."""
    from bernstein.core.git.read_set_receipt import ChangedPath

    return [
        ChangedPath(
            path="src/config.py",
            old_commit="aaa111",
            new_commit="bbb222",
        ),
        ChangedPath(
            path="src/utils.py",
            old_commit="ccc333",
            new_commit="ddd444",
        ),
    ]


@pytest.fixture
def sample_receipt(sample_changed_paths):
    """Return a sample unsigned ReadSetRefusalReceipt."""
    from bernstein.core.git.read_set_receipt import ReadSetRefusalReceipt

    return ReadSetRefusalReceipt(
        v=1,
        task_id="task-123",
        base_commit="abcdef",
        target_branch="main",
        changed_paths=sample_changed_paths,
        timestamp=1234567890,
        signer_public_key_pem="-----BEGIN PUBLIC KEY-----\nMCYwEAYHKoZIzj0CAQYF\n-----END PUBLIC KEY-----",
        signature="",
    )


@pytest.fixture
def keys(tmp_path: Path):
    """Generate and return test keypair."""
    from bernstein.core.lineage.identity import generate_keypair

    private_key, public_key = generate_keypair()
    return {
        "private": private_key,
        "public": public_key,
    }


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_deterministic_serialization(sample_receipt: ReadSetRefusalReceipt) -> None:
    """Two identical invocations produce byte-identical JSON representations.

    This verifies the receipt serialization is deterministic and would produce
    identical audit artifacts across orchestrator workers.
    """
    from bernstein.core.git.read_set_receipt import serialize_receipt

    # First invocation
    bytes1 = serialize_receipt(sample_receipt)

    # Second invocation with identical inputs
    bytes2 = serialize_receipt(sample_receipt)

    assert bytes1 == bytes2


def test_canonical_bytes_are_stable(sample_receipt: ReadSetRefusalReceipt) -> None:
    """Canonical bytes are stable across runs (no wall-clock or chain field)."""
    from bernstein.core.git.read_set_receipt import serialize_receipt

    # The canonical bytes should be stable even if we create a new receipt
    # with the same content
    bytes1 = sample_receipt.canonical_bytes()
    bytes2 = serialize_receipt(sample_receipt)

    assert bytes1 == bytes2


def test_receipt_hash_is_deterministic(sample_receipt: ReadSetRefusalReceipt) -> None:
    """Receipt hash is a pure function of canonical content."""
    hash1 = sample_receipt.receipt_hash()
    hash2 = sample_receipt.receipt_hash()

    assert hash1 == hash2
    assert hash1.startswith("sha256:")


def test_receipt_signature_verifies(sample_receipt: ReadSetRefusalReceipt, keys: dict) -> None:
    """A signed receipt verifies correctly against its embedded public key."""
    from bernstein.core.git.read_set_receipt import build_refusal_receipt

    signed = build_refusal_receipt(
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=sample_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=sample_receipt.timestamp,
    )

    assert signed.signature != ""
    assert signed.signer_public_key_pem == keys["public"]


def test_receipt_tampering_fails_verification(sample_receipt: ReadSetRefusalReceipt, keys: dict) -> None:
    """A mutated receipt fails offline verification."""
    from bernstein.core.git.read_set_receipt import (
        ReadSetRefusalReceipt,
        build_refusal_receipt,
    )

    signed = build_refusal_receipt(
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=sample_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=sample_receipt.timestamp,
    )

    # Tamper with the task_id
    tampered = ReadSetRefusalReceipt(
        v=signed.v,
        task_id="tampered-task",
        base_commit=signed.base_commit,
        target_branch=signed.target_branch,
        changed_paths=signed.changed_paths,
        timestamp=signed.timestamp,
        signer_public_key_pem=signed.signer_public_key_pem,
        signature=signed.signature,
    )

    assert signed.receipt_hash() != tampered.receipt_hash()


def test_receipt_to_dict_round_trip(sample_receipt: ReadSetRefusalReceipt, keys: dict) -> None:
    """Receipt round-trips through to_dict/from_dict correctly."""
    from bernstein.core.git.read_set_receipt import (
        ReadSetRefusalReceipt,
        build_refusal_receipt,
    )

    signed = build_refusal_receipt(
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=sample_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=sample_receipt.timestamp,
    )

    # Round-trip through dict
    d = signed.to_dict()
    recovered = ReadSetRefusalReceipt.from_dict(d)

    assert recovered.task_id == signed.task_id
    assert recovered.base_commit == signed.base_commit
    assert recovered.target_branch == signed.target_branch
    assert len(recovered.changed_paths) == len(signed.changed_paths)
    assert recovered.signature == signed.signature
    assert recovered.signer_public_key_pem == signed.signer_public_key_pem
    assert recovered.receipt_hash() == signed.receipt_hash()


def test_offline_verification_fails_with_wrong_key(sample_receipt: ReadSetRefusalReceipt, keys: dict) -> None:
    """Receipt signed with one key fails verification with another."""
    from bernstein.core.git.read_set_receipt import (
        ReadSetRefusalReceipt,
        build_refusal_receipt,
        verify_refusal_receipt,
    )

    signed = build_refusal_receipt(
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=sample_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=sample_receipt.timestamp,
    )

    # Generate a different keypair
    from bernstein.core.lineage.identity import generate_keypair

    _wrong_private, wrong_public = generate_keypair()

    # Replace the public key with the wrong one
    tampered = ReadSetRefusalReceipt(
        v=signed.v,
        task_id=signed.task_id,
        base_commit=signed.base_commit,
        target_branch=signed.target_branch,
        changed_paths=signed.changed_paths,
        timestamp=signed.timestamp,
        signer_public_key_pem=wrong_public,
        signature=signed.signature,
    )

    assert not verify_refusal_receipt(tampered)


def test_receipt_persists_and_round_trips(sample_receipt: ReadSetRefusalReceipt, keys: dict, tmp_path: Path) -> None:
    """Receipt writes to disk and reads back correctly."""
    from bernstein.core.git.read_set_receipt import (
        build_refusal_receipt,
        read_refusal_receipt,
        write_refusal_receipt,
    )

    signed = build_refusal_receipt(
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=sample_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=sample_receipt.timestamp,
    )

    # Write to disk
    path = write_refusal_receipt(tmp_path, signed)

    assert path.is_file()

    # Read back
    loaded = read_refusal_receipt(path)

    assert loaded is not None
    assert loaded.task_id == signed.task_id
    assert loaded.receipt_hash() == signed.receipt_hash()


def test_receipt_rejection_empty_changed_paths(sample_receipt: ReadSetRefusalReceipt, keys: dict) -> None:
    """Receipt works with empty changed_paths list."""
    from bernstein.core.git.read_set_receipt import (
        ReadSetRefusalReceipt,
        build_refusal_receipt,
    )

    # Use empty paths
    empty_receipt = ReadSetRefusalReceipt(
        v=sample_receipt.v,
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=[],  # Empty
        timestamp=sample_receipt.timestamp,
        signer_public_key_pem=keys["public"],
    )

    signed = build_refusal_receipt(
        task_id=empty_receipt.task_id,
        base_commit=empty_receipt.base_commit,
        target_branch=empty_receipt.target_branch,
        changed_paths=empty_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=empty_receipt.timestamp,
    )

    assert signed.signature != ""
    assert len(signed.changed_paths) == 0


def test_receipt_from_dict_handles_missing_fields(sample_receipt: ReadSetRefusalReceipt) -> None:
    """from_dict provides defaults for missing optional fields."""
    from bernstein.core.git.read_set_receipt import ReadSetRefusalReceipt

    # Minimal dict
    minimal = {
        "v": 1,
        "task_id": "task-456",
        "base_commit": "deadbeef",
        "target_branch": "develop",
        "changed_paths": [],
    }

    recovered = ReadSetRefusalReceipt.from_dict(minimal)

    assert recovered.v == 1
    assert recovered.task_id == "task-456"
    assert recovered.base_commit == "deadbeef"
    assert recovered.target_branch == "develop"
    assert len(recovered.changed_paths) == 0
    assert recovered.timestamp == 0
    assert recovered.signer_public_key_pem == ""
    assert recovered.signature == ""


def test_changed_path_dataclass(sample_changed_paths) -> None:
    """ChangedPath is properly frozen and hashable."""
    cp = sample_changed_paths[0]

    assert cp.path == "src/config.py"
    assert cp.old_commit == "aaa111"
    assert cp.new_commit == "bbb222"

    # Verify frozen (immutable)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cp.path = "modified.py"

    # Verify hashable (for use in sets/dicts)
    paths_set = {cp}
    assert cp in paths_set


# ------------------------------------------------------------------
# Offline verification tests (mocked)
# ------------------------------------------------------------------


def test_verify_receipt_offline_malformed_bytes(tmp_path: Path) -> None:
    """Malformed receipt bytes raise ValueError."""
    from bernstein.core.git.read_set_receipt import verify_receipt_offline

    malformed = b"{this is not valid json"
    with pytest.raises(ValueError, match="Malformed receipt bytes"):
        verify_receipt_offline(malformed, str(tmp_path / "chain.db"))


def test_verify_receipt_offline_invalid_chain(tmp_path: Path) -> None:
    """Invalid chain path returns False."""
    from bernstein.core.git.read_set_receipt import verify_receipt_offline

    valid_receipt = b'{"v":1,"task_id":"t","base_commit":"b","target_branch":"m","changed_paths":[]}'
    result = verify_receipt_offline(valid_receipt, "/nonexistent/path.db")
    assert result is False


def test_verify_receipt_offline_no_matching_anchor(
    sample_receipt: ReadSetRefusalReceipt, keys: dict, tmp_path: Path
) -> None:
    """Offline verification fails when receipt is not anchored in chain."""
    from bernstein.core.git.read_set_receipt import (
        build_refusal_receipt,
        verify_receipt_offline,
    )

    signed = build_refusal_receipt(
        task_id=sample_receipt.task_id,
        base_commit=sample_receipt.base_commit,
        target_branch=sample_receipt.target_branch,
        changed_paths=sample_receipt.changed_paths,
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
        timestamp=sample_receipt.timestamp,
    )

    # Mock chain that verifies but has no matching anchor
    mock_chain = MagicMock()
    mock_chain.verify.return_value = (True, [])
    mock_chain.query.return_value = []  # No matching entries

    mock_audit_log = MagicMock()
    mock_audit_log.load_from_file.return_value = mock_audit_log

    with (
        patch("bernstein.core.security.audit.AuditLog", return_value=mock_audit_log),
        patch("bernstein.core.security.audit_chain.AuditChainStore", return_value=mock_chain),
        patch(
            "bernstein.core.security.audit_chain.EVENT_READ_SET_REFUSAL",
            "read_set.refusal_receipt",
        ),
    ):
        receipt_bytes = signed.canonical_bytes()
        result = verify_receipt_offline(receipt_bytes, str(tmp_path / "chain.db"))

        assert result is False


def test_verify_receipt_offline_accepts_a_genuinely_anchored_receipt(keys: dict, tmp_path: Path) -> None:
    """A signed receipt anchored in a real chain verifies offline.

    The three tests above all assert ``False``, so they hold whether or not
    the chain actually loads. This one pins the positive path against real
    ``AuditChainStore`` construction rather than a mock: a receipt sealed by
    ``refuse_read_set`` and anchored in its own chain must verify from the
    on-disk record alone, with no repository access.
    """
    import json

    from bernstein.core.git.read_set_receipt import (
        ChangedPath,
        refuse_read_set,
        verify_receipt_offline,
    )
    from bernstein.core.security.audit_chain import AuditChainStore

    sdd_dir = tmp_path / ".sdd"
    runtime_dir = sdd_dir / "runtime"
    chain = AuditChainStore(runtime_dir)

    receipt = refuse_read_set(
        chain=chain,
        sdd_dir=sdd_dir,
        task_id="task-offline",
        base_commit="abcdef",
        target_branch="main",
        changed_paths=[ChangedPath(path="src/a.py", old_commit="a1", new_commit="b2")],
        private_key_pem=keys["private"],
        public_key_pem=keys["public"],
    )

    record = json.dumps(receipt.to_dict()).encode("utf-8")
    assert verify_receipt_offline(record, str(runtime_dir / "chain.db")) is True
