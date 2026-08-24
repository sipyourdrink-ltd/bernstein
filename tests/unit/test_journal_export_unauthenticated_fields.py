"""Tests for ReceiptManifest.unauthenticated_fields field."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from bernstein.core.persistence.journal import Journal
from bernstein.core.persistence.journal_export import (
    _UNAUTHENTICATED_FIELDS,
    ReceiptManifest,
    export_receipt,
    verify_receipt,
)


def _populate(agent_dir: Path, n_steps: int = 3) -> None:
    journal = Journal.open(agent_dir)
    for i in range(n_steps):
        journal.append(
            input_hash=f"a{i}",
            model="m1",
            prompt=f"prompt {i}",
            tool_call={"name": "echo", "args": {"x": i}},
            tool_result={"ok": True, "stdout": f"out {i}"},
        )
    journal.close()


class TestReceiptManifestUnauthenticatedFields:
    def test_receipt_manifest_default_unauthenticated_fields(self) -> None:
        """ReceiptManifest default unauthenticated_fields matches _UNAUTHENTICATED_FIELDS."""
        manifest = ReceiptManifest(
            agent_id="test-agent",
            head_hash="a" * 64,
            steps=3,
            bernstein_version="test",
            created_at="2024-01-01T00:00:00Z",
        )
        assert manifest.unauthenticated_fields == _UNAUTHENTICATED_FIELDS
        assert isinstance(manifest.unauthenticated_fields, list)

    def test_canonical_bytes_includes_unauthenticated_fields(self) -> None:
        """canonical_bytes() includes unauthenticated_fields in the signed document."""
        manifest = ReceiptManifest(
            agent_id="test-agent",
            head_hash="a" * 64,
            steps=3,
            bernstein_version="test",
            created_at="2024-01-01T00:00:00Z",
        )
        canonical = json.loads(manifest.canonical_bytes())
        assert "unauthenticated_fields" in canonical
        assert canonical["unauthenticated_fields"] == _UNAUTHENTICATED_FIELDS

    def test_to_json_includes_unauthenticated_fields(self) -> None:
        """to_json() includes unauthenticated_fields in pretty output."""
        manifest = ReceiptManifest(
            agent_id="test-agent",
            head_hash="a" * 64,
            steps=3,
            bernstein_version="test",
            created_at="2024-01-01T00:00:00Z",
        )
        pretty = json.loads(manifest.to_json())
        assert "unauthenticated_fields" in pretty
        assert pretty["unauthenticated_fields"] == _UNAUTHENTICATED_FIELDS

    def test_from_json_bytes_round_trips_unauthenticated_fields(self) -> None:
        """from_json_bytes() round-trips unauthenticated_fields correctly."""
        original = ReceiptManifest(
            agent_id="test-agent",
            head_hash="a" * 64,
            steps=3,
            bernstein_version="test",
            created_at="2024-01-01T00:00:00Z",
        )
        json_bytes = original.to_json().encode("utf-8")
        restored = ReceiptManifest.from_json_bytes(json_bytes)
        assert restored.unauthenticated_fields == _UNAUTHENTICATED_FIELDS

    def test_from_json_bytes_falls_back_when_key_missing(self) -> None:
        """from_json_bytes() falls back to _UNAUTHENTICATED_FIELDS when key is absent."""
        # Create JSON without unauthenticated_fields key (simulating old manifest)
        old_manifest_json = json.dumps(
            {
                "agent_id": "test-agent",
                "head_hash": "a" * 64,
                "steps": 3,
                "bernstein_version": "test",
                "created_at": "2024-01-01T00:00:00Z",
                "blob_digests": [],
                "format_version": 1,
                # unauthenticated_fields intentionally omitted
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        restored = ReceiptManifest.from_json_bytes(old_manifest_json)
        assert restored.unauthenticated_fields == _UNAUTHENTICATED_FIELDS

    def test_export_receipt_manifest_contains_unauthenticated_fields(self, tmp_path: Path) -> None:
        """export_receipt writes manifest.json with unauthenticated_fields key."""
        agent_dir = tmp_path / "agent-1"
        _populate(agent_dir, n_steps=2)
        receipt_path = tmp_path / "receipt.tar"

        export_receipt(agent_dir, receipt_path, agent_id="agent-1")

        # Extract and read manifest.json from tarball
        with tarfile.open(receipt_path, "r") as tar:
            manifest_member = tar.getmember("manifest.json")
            manifest_bytes = tar.extractfile(manifest_member).read()

        manifest_data = json.loads(manifest_bytes)
        assert "unauthenticated_fields" in manifest_data
        assert manifest_data["unauthenticated_fields"] == _UNAUTHENTICATED_FIELDS


class TestReceiptRoundTripUnauthenticatedFields:
    def test_export_receipt_manifest_contains_unauthenticated_fields_e2e(self, tmp_path: Path) -> None:
        """End-to-end: export -> verify preserves manifest with unauthenticated_fields."""
        agent_dir = tmp_path / "agent-1"
        _populate(agent_dir, n_steps=2)
        receipt_path = tmp_path / "receipt.tar"

        export_receipt(agent_dir, receipt_path, agent_id="agent-1")

        result = verify_receipt(receipt_path)

        assert result.ok
        # The unauthenticated_fields are in the manifest inside the receipt tarball
        # verify_receipt reads the manifest but doesn't currently surface the field
        # in ReceiptVerificationResult. This test validates the manifest round-trips.
        with tarfile.open(receipt_path, "r") as tar:
            manifest_member = tar.getmember("manifest.json")
            manifest_bytes = tar.extractfile(manifest_member).read()

        manifest_data = json.loads(manifest_bytes)
        assert "unauthenticated_fields" in manifest_data
        assert manifest_data["unauthenticated_fields"] == _UNAUTHENTICATED_FIELDS
