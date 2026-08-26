import pytest
from bernstein.core.tasks.artifacts import (
    ArtifactKind,
    ArtifactSpec,
    canonicalise_artifact,
    content_hash,
    parse_artifact_spec,
)
from bernstein.core.tasks.artifact_completion import (
    ArtifactCompletion,
    is_artifact_mode,
    load_artifact,
    complete_artifact_task,
)
from bernstein.core.tasks.models import Task
from pathlib import Path
import tempfile
import os


def test_blob_artifact_roundtrips_declare_complete_verify():
    """Test that a blob artifact can be declared, completed, and verified."""
    # Create a temporary directory for the task
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        # Create a test blob file
        blob_content = b"hello world blob"
        artifact_path = workdir / "test_blob.bin"
        artifact_path.write_bytes(blob_content)

        # Create a task that declares a blob artifact
        task = Task(
            id="test-task-1",
            title="Test Blob Task",
            description="Test blob artifact completion",
            role="backend",
            spec={
                "artifact_spec": {
                    "kind": "blob",
                    "output_path": "test_blob.bin",
                }
            },
        )

        # Verify the task is in artifact mode
        assert is_artifact_mode(task)

        # Run artifact completion
        completion = complete_artifact_task(task, workdir)

        # Check that the completion succeeded
        assert completion.ok
        assert completion.receipt is not None
        # The receipt should contain the content hash of the blob
        expected_hash = content_hash(blob_content)
        assert completion.receipt["content_hash"] == expected_hash

        # Verify the artifact using the receipt
        from bernstein.core.lineage.artifact_record import verify_artifact
        sdd_dir = workdir / ".sdd"
        verification_result = verify_artifact(completion.receipt, sdd_dir)
        assert verification_result.verified


def test_one_byte_mutation_fails_blob_verification():
    """Test that a one-byte mutation causes blob verification to fail."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        # Create a test blob file
        blob_content = b"hello world blob"
        artifact_path = workdir / "test_blob.bin"
        artifact_path.write_bytes(blob_content)

        # Create a task that declares a blob artifact
        task = Task(
            id="test-task-2",
            title="Test Blob Task Mutation",
            description="Test blob artifact completion with mutation",
            role="backend",
            spec={
                "artifact_spec": {
                    "kind": "blob",
                    "output_path": "test_blob.bin",
                }
            },
        )

        # Run artifact completion
        completion = complete_artifact_task(task, workdir)
        assert completion.ok
        assert completion.receipt is not None

        # Mutate the blob by flipping one bit
        mutated_content = bytearray(blob_content)
        mutated_content[0] ^= 0x01  # Flip the first bit
        artifact_path.write_bytes(bytes(mutated_content))

        # Verify the artifact should fail
        from bernstein.core.lineage.artifact_record import verify_artifact
        sdd_dir = workdir / ".sdd"
        verification_result = verify_artifact(completion.receipt, sdd_dir)
        assert not verification_result.verified


def test_text_criteria_on_blob_kind_rejected_at_parse_time():
    """Test that text-specific criteria (schema_valid, criteria_match) are rejected for blob kind at parse time."""
    # Test schema_valid criterion on blob kind
    with pytest.raises(Exception) as exc_info:
        parse_artifact_spec(
            {
                "artifact_spec": {
                    "kind": "blob",
                    "output_path": "test.bin",
                    "criteria": [{"type": "schema_valid", "value": '{"type": "object"}'}],
                }
            }
        )
    assert "schema_valid" in str(exc_info.value)
    assert "not supported for blob kind" in str(exc_info.value)

    # Test criteria_match criterion on blob kind
    with pytest.raises(Exception) as exc_info:
        parse_artifact_spec(
            {
                "artifact_spec": {
                    "kind": "blob",
                    "output_path": "test.bin",
                    "criteria": [{"type": "criteria_match", "value": '[{"path": "test", "op": "exists"}]'}],
                }
            }
        )
    assert "criteria_match" in str(exc_info.value)
    assert "not supported for blob kind" in str(exc_info.value)


def test_blob_bytes_are_content_addressed_in_evidence_store():
    """Test that blob bytes are stored content-addressed in the evidence store."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        # Create two different blob files with same content
        blob_content = b"identical content"
        artifact_path1 = workdir / "test_blob1.bin"
        artifact_path2 = workdir / "test_blob2.bin"
        artifact_path1.write_bytes(blob_content)
        artifact_path2.write_bytes(blob_content)

        # Create two tasks that declare blob artifacts with same content but different paths
        task1 = Task(
            id="test-task-3",
            name="Test Blob Task 1",
            role="backend",
            spec={
                "artifact_spec": {
                    "kind": "blob",
                    "output_path": "test_blob1.bin",
                }
            },
        )
        task2 = Task(
            id="test-task-4",
            name="Test Blob Task 2",
            role="backend",
            spec={
                "artifact_spec": {
                    "kind": "blob",
                    "output_path": "test_blob2.bin",
                }
            },
        )

        # Run artifact completion for both
        completion1 = run_artifact_completion(task1, workdir)
        completion2 = run_artifact_completion(task2, workdir)

        assert completion1.ok
        assert completion2.ok
        assert completion1.receipt is not None
        assert completion2.receipt is not None

        # The content hashes should be the same
        assert completion1.receipt["content_hash"] == completion2.receipt["content_hash"]
        # And they should match the hash of the content
        expected_hash = content_hash(blob_content)
        assert completion1.receipt["content_hash"] == expected_hash
        assert completion2.receipt["content_hash"] == expected_hash