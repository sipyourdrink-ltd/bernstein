"""Unit tests for evolution rollback receipt."""

import hashlib
import json
import tempfile
import time
from pathlib import Path

from bernstein.evolution.rollback_receipt import (
    build_rollback_receipt,
    read_rollback_receipt,
    rollback_receipt_path,
    verify_rollback_receipt,
    write_rollback_receipt,
)


def test_rollback_receipt_hash_recomputes_and_verifies():
    """Test that a rollback receipt can be built, written, read, and verified."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        lineage_root = workdir / ".sdd" / "lineage"
        lineage_root.mkdir(parents=True)
        hmac_key = b"0123456789abcdef" * 4  # 32 bytes for HMAC-SHA256

        # Create file content and compute digests
        content1 = b"content of file1"
        digest1 = "sha256:" + hashlib.sha256(content1).hexdigest()
        content2 = b"content of file2"
        digest2 = "sha256:" + hashlib.sha256(content2).hexdigest()

        # Build a receipt
        proposal_id = "prop-123"
        proposal_title = "Test Proposal"
        manifest_digest = "sha256:" + "a" * 64
        restored_files = {
            "src/file1.txt": digest1,
            "src/file2.txt": digest2,
        }
        status = "success"
        timestamp = str(int(time.time()))

        # Write the files to the workdir
        (workdir / "src").mkdir(parents=True, exist_ok=True)
        (workdir / "src" / "file1.txt").write_bytes(content1)
        (workdir / "src" / "file2.txt").write_bytes(content2)

        receipt = build_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            proposal_id=proposal_id,
            proposal_title=proposal_title,
            manifest_digest=manifest_digest,
            restored_files=restored_files,
            status=status,
            timestamp=timestamp,
        )

        # Check that the receipt hash is computed from the body
        assert receipt.receipt_hash.startswith("sha256:")
        assert len(receipt.receipt_hash) == 71  # "sha256:" + 64 hex digits

        # Write the receipt to disk
        write_rollback_receipt(workdir, receipt)

        # Read it back
        read_receipt = read_rollback_receipt(workdir, receipt.receipt_hash)
        assert read_receipt is not None
        assert read_receipt == receipt

        # Verify the receipt
        verified = verify_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            receipt_hash=receipt.receipt_hash,
        )
        assert verified.ok, f"Verification failed: {verified.reason}"
        assert verified.receipt is not None
        assert verified.receipt.receipt_hash == receipt.receipt_hash


def test_rollback_receipt_tampering_fails():
    """Test that tampering with a receipt field causes verification to fail."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        lineage_root = workdir / ".sdd" / "lineage"
        lineage_root.mkdir(parents=True)
        hmac_key = b"0123456789abcdef" * 4

        # Build a receipt
        receipt = build_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            proposal_id="prop-123",
            proposal_title="Test Proposal",
            manifest_digest="sha256:" + "a" * 64,
            restored_files={"src/file1.txt": "sha256:" + "b" * 64},
            status="success",
            timestamp=str(int(time.time())),
        )

        # Write the receipt
        write_rollback_receipt(workdir, receipt)

        # Tamper with the stored file: change the proposal_title
        path = rollback_receipt_path(workdir, receipt.receipt_hash)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["proposal_title"] = "Tampered Title"
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        # Now verify: should fail because the receipt hash won't match
        verified = verify_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            receipt_hash=receipt.receipt_hash,
        )
        assert not verified.ok
        assert "receipt_hash does not recompute from the receipt body" in verified.reason


def test_rollback_receipt_missing_file_fails():
    """Test that verifying a non-existent receipt fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        lineage_root = workdir / ".sdd" / "lineage"
        lineage_root.mkdir(parents=True)
        hmac_key = b"0123456789abcdef" * 4

        fake_hash = "sha256:" + "0" * 64
        verified = verify_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            receipt_hash=fake_hash,
        )
        assert not verified.ok
        assert f"no rollback receipt for {fake_hash!r}" in verified.reason


def test_rollback_receipt_restored_file_mismatch_fails():
    """Test that if a restored file's digest doesn't match, verification fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        lineage_root = workdir / ".sdd" / "lineage"
        lineage_root.mkdir(parents=True)
        hmac_key = b"0123456789abcdef" * 4

        # Create a file that we will later restore
        file_path = workdir / "src" / "file1.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("original content", encoding="utf-8")

        # Build a receipt that claims to have restored the file to a different digest
        receipt = build_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            proposal_id="prop-123",
            proposal_title="Test Proposal",
            manifest_digest="sha256:" + "a" * 64,
            restored_files={"src/file1.txt": "sha256:" + "b" * 64},  # wrong digest
            status="success",
            timestamp=str(int(time.time())),
        )

        # Write the receipt
        write_rollback_receipt(workdir, receipt)

        # Verify: should fail because the file digest doesn't match
        verified = verify_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            receipt_hash=receipt.receipt_hash,
        )
        assert not verified.ok
        assert "restored file digest mismatch for src/file1.txt" in verified.reason


def test_rollback_receipt_restored_file_missing_fails():
    """Test that if a restored file is missing, verification fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        lineage_root = workdir / ".sdd" / "lineage"
        lineage_root.mkdir(parents=True)
        hmac_key = b"0123456789abcdef" * 4

        # Build a receipt that claims to have restored a file that doesn't exist
        receipt = build_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            proposal_id="prop-123",
            proposal_title="Test Proposal",
            manifest_digest="sha256:" + "a" * 64,
            restored_files={"src/missing.txt": "sha256:" + "b" * 64},
            status="success",
            timestamp=str(int(time.time())),
        )

        # Write the receipt
        write_rollback_receipt(workdir, receipt)

        # Verify: should fail because the file is missing
        verified = verify_rollback_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            receipt_hash=receipt.receipt_hash,
        )
        assert not verified.ok
        assert "restored file missing at src/missing.txt" in verified.reason


if __name__ == "__main__":
    # Run the tests
    test_rollback_receipt_hash_recomputes_and_verifies()
    test_rollback_receipt_tampering_fails()
    test_rollback_receipt_missing_file_fails()
    test_rollback_receipt_restored_file_mismatch_fails()
    test_rollback_receipt_restored_file_missing_fails()
    print("All tests passed.")
