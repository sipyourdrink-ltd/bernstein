"""Tests for CAS garbage collection (bernsstein.core.persistence.cas_gc)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.persistence.cas_gc import (
    _extract_digests_from_obj,
    _scan_audit_seals_for_digests,
    _scan_backlog_for_digests,
    _scan_lineage_for_digests,
    _scan_wal_for_digests,
    collect_referenced_digests,
    prune_cas_store,
)
from bernstein.core.persistence.cas_store import CASStore


class TestExtractDigestsFromObj:
    """Tests for _extract_digests_from_obj helper."""

    def test_extract_from_string_with_digest(self) -> None:
        """Extract 64-char hex digest from a string."""
        text = "some text 0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12 more"
        digests = _extract_digests_from_obj(text)
        assert len(digests) == 1
        assert "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12" in digests

    def test_extract_from_string_without_digest(self) -> None:
        """Return empty set when no digest present."""
        text = "just some random text"
        digests = _extract_digests_from_obj(text)
        assert digests == set()

    def test_extract_from_dict(self) -> None:
        """Extract digests from dict values."""
        data = {"digest": "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12"}
        digests = _extract_digests_from_obj(data)
        assert len(digests) == 1

    def test_extract_from_nested_dict(self) -> None:
        """Extract digests from nested dict."""
        data = {"outer": {"inner": {"digest": "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12"}}}
        digests = _extract_digests_from_obj(data)
        assert len(digests) == 1

    def test_extract_from_list(self) -> None:
        """Extract digests from list items."""
        data = ["first", "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12", "last"]
        digests = _extract_digests_from_obj(data)
        assert len(digests) == 1

    def test_extract_multiple_digests(self) -> None:
        """Extract multiple digests from same string."""
        text = "digests: 0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12 and 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a23"
        digests = _extract_digests_from_obj(text)
        assert len(digests) == 2

    def test_extract_non_hex_ignored(self) -> None:
        """Strings that look like hex but not 64 chars are ignored."""
        text = "not-a-digest: 0a1b2c"  # Too short
        digests = _extract_digests_from_obj(text)
        assert digests == set()

    def test_extract_mixed_case_normalized(self) -> None:
        """Digests are normalized to lowercase."""
        text = "DIGEST: 0A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A9B0C1D2E3F4A5B6C7D8E9F0A12"
        digests = _extract_digests_from_obj(text)
        assert "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12" in digests


class TestScanWALForDigests:
    """Tests for _scan_wal_for_digests."""

    def test_scan_empty_wal_dir(self, tmp_path: Path) -> None:
        """Empty WAL directory returns empty set."""
        result = _scan_wal_for_digests(tmp_path)
        assert result == set()

    def test_scan_wal_with_digests(self, tmp_path: Path) -> None:
        """WAL entries containing digests are extracted."""
        wal_dir = tmp_path / "runtime" / "wal"
        wal_dir.mkdir(parents=True)

        wal_file = wal_dir / "test.wal.jsonl"
        wal_file.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "inputs": {"digest": "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12"},
                    "output": {"result": "ok"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = _scan_wal_for_digests(tmp_path)
        assert len(result) == 1

    def test_scan_multiple_wal_files(self, tmp_path: Path) -> None:
        """All WAL files are scanned."""
        wal_dir = tmp_path / "runtime" / "wal"
        wal_dir.mkdir(parents=True)

        for i in range(3):
            wal_file = wal_dir / f"run-{i}.wal.jsonl"
            wal_file.write_text(
                json.dumps({"seq": 1, "inputs": {"digest": f"{i:0>64}"}}) + "\n",
                encoding="utf-8",
            )

        result = _scan_wal_for_digests(tmp_path)
        assert len(result) == 3


class TestScanAuditSealsForDigests:
    """Tests for _scan_audit_seals_for_digests."""

    def test_scan_empty_audit_dir(self, tmp_path: Path) -> None:
        """Empty audit directory returns empty set."""
        result = _scan_audit_seals_for_digests(tmp_path)
        assert result == set()

    def test_scan_seal_with_leaves(self, tmp_path: Path) -> None:
        """Audit seal leaves are extracted as digests."""
        audit_dir = tmp_path / "audit" / "merkle"
        audit_dir.mkdir(parents=True)

        seal_file = audit_dir / "seal-2024-01-01T00-00-00.json"
        seal_data = {
            "root_hash": "root-hash-value",
            "leaves": [
                "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12",
                "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a23",
            ],
        }
        seal_file.write_text(json.dumps(seal_data), encoding="utf-8")

        result = _scan_audit_seals_for_digests(tmp_path)
        assert len(result) == 2


class TestScanLineageForDigests:
    """Tests for _scan_lineage_for_digests."""

    def test_scan_empty_lineage_dir(self, tmp_path: Path) -> None:
        """Empty lineage directory returns empty set."""
        result = _scan_lineage_for_digests(tmp_path)
        assert result == set()

    def test_scan_lineage_with_content_hash(self, tmp_path: Path) -> None:
        """Lineage spine content_hash values are extracted."""
        lineage_dir = tmp_path / "lineage" / "run-1"
        lineage_dir.mkdir(parents=True)

        spine_file = lineage_dir / "spine.jsonl"
        spine_file.write_text(
            json.dumps(
                {
                    "v": 2,
                    "content_hash": "sha256:0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12",
                    "artifact_path": "output.txt",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = _scan_lineage_for_digests(tmp_path)
        assert len(result) == 1
        assert "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12" in result

    def test_scan_multiple_lineage_files(self, tmp_path: Path) -> None:
        """All lineage spine files are scanned."""
        lineage_dir = tmp_path / "lineage"
        lineage_dir.mkdir(parents=True)

        for i in range(2):
            run_dir = lineage_dir / f"run-{i}"
            run_dir.mkdir(parents=True)
            spine_file = run_dir / "spine.jsonl"
            spine_file.write_text(
                json.dumps(
                    {
                        "v": 2,
                        "content_hash": f"sha256:{i:0>64}",
                        "artifact_path": f"output-{i}.txt",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        result = _scan_lineage_for_digests(tmp_path)
        assert len(result) == 2


class TestScanBacklogForDigests:
    """Tests for _scan_backlog_for_digests."""

    def test_scan_empty_backlog_dir(self, tmp_path: Path) -> None:
        """Empty backlog directory returns empty set."""
        result = _scan_backlog_for_digests(tmp_path)
        assert result == set()

    def test_scan_backlog_yaml_with_digest(self, tmp_path: Path) -> None:
        """Backlog YAML containing digests are extracted."""
        backlog_dir = tmp_path / "backlog" / "open"
        backlog_dir.mkdir(parents=True)

        yaml_file = backlog_dir / "task-1.yaml"
        yaml_file.write_text(
            "title: Test task\ncontent: 0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12\n",
            encoding="utf-8",
        )

        result = _scan_backlog_for_digests(tmp_path)
        assert len(result) == 1


class TestCollectReferencedDigests:
    """Tests for collect_referenced_digests."""

    def test_collect_from_all_roots(self, tmp_path: Path) -> None:
        """Digests from all roots are collected."""
        # Setup WAL
        wal_dir = tmp_path / "runtime" / "wal"
        wal_dir.mkdir(parents=True)
        wal_file = wal_dir / "run.wal.jsonl"
        wal_file.write_text(
            json.dumps(
                {"seq": 1, "inputs": {"digest": "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a12"}}
            )
            + "\n",
            encoding="utf-8",
        )

        # Setup lineage
        lineage_dir = tmp_path / "lineage" / "run-1"
        lineage_dir.mkdir(parents=True)
        spine_file = lineage_dir / "spine.jsonl"
        spine_file.write_text(
            json.dumps(
                {"v": 2, "content_hash": "sha256:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a23"}
            )
            + "\n",
            encoding="utf-8",
        )

        result = collect_referenced_digests(tmp_path)
        assert len(result) == 2


class TestPruneCASStore:
    """Tests for prune_cas_store."""

    def test_empty_cas_store(self, tmp_path: Path) -> None:
        """Empty CAS store returns zero counts."""
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()

        result = prune_cas_store(sdd_dir, retention_days=30)
        assert result.scanned_entries == 0
        assert result.deleted_entries == 0
        assert result.preserved_entries == 0

    def test_referenced_digest_survives(self, tmp_path: Path) -> None:
        """AC1: A referenced digest survives GC."""
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()

        # Store the referenced blob
        store = CASStore(cas_dir)
        referenced_digest = store.put(b"referenced-content", content_type="text/plain")

        # Create a referenced entry via WAL
        wal_dir = sdd_dir / "runtime" / "wal"
        wal_dir.mkdir(parents=True)
        wal_file = wal_dir / "run.wal.jsonl"
        wal_file.write_text(
            json.dumps({"seq": 1, "inputs": {"digest": referenced_digest}}) + "\n",
            encoding="utf-8",
        )

        # Also add an unreferenced blob that is older than the retention window
        unreferenced_digest = store.put(b"unreferenced-content", content_type="text/plain")
        old_meta_path = cas_dir / unreferenced_digest[:2] / f"{unreferenced_digest}.meta.json"
        meta_data = json.loads(old_meta_path.read_text(encoding="utf-8"))
        meta_data["created_at"] = time.time() - (31 * 86400)
        old_meta_path.write_text(json.dumps(meta_data, indent=2) + "\n", encoding="utf-8")

        # Verify both exist
        assert store.has(unreferenced_digest)
        assert store.has(referenced_digest)

        # Run GC
        result = prune_cas_store(sdd_dir, retention_days=30)

        # Referenced blob should be preserved, unreferenced should be deleted
        assert result.preserved_entries == 1
        assert result.deleted_entries == 1
        assert store.has(referenced_digest)
        assert not store.has(unreferenced_digest)

    def test_unreferenced_young_survives(self, tmp_path: Path) -> None:
        """AC2: An unreferenced digest younger than window survives."""
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()

        store = CASStore(cas_dir)
        unreferenced_digest = store.put(b"young-unreferenced", content_type="text/plain")

        # Run GC with 30-day retention
        result = prune_cas_store(sdd_dir, retention_days=30)

        # Young unreferenced blob should be preserved
        assert result.preserved_entries == 1
        assert result.deleted_entries == 0
        assert store.has(unreferenced_digest)

    def test_unreferenced_old_deleted(self, tmp_path: Path) -> None:
        """AC2: An unreferenced digest older than window is removed."""
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()

        store = CASStore(cas_dir)
        old_digest = store.put(b"old-content", content_type="text/plain")

        # Override the created_at by rewriting metadata
        meta_path = cas_dir / old_digest[:2] / f"{old_digest}.meta.json"
        meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        meta_data["created_at"] = time.time() - (31 * 86400)
        meta_path.write_text(json.dumps(meta_data, indent=2) + "\n", encoding="utf-8")

        # Run GC with 30-day retention
        result = prune_cas_store(sdd_dir, retention_days=30)

        # Old unreferenced blob should be deleted
        assert result.deleted_entries == 1
        assert result.deleted_bytes == len(b"old-content")
        assert not store.has(old_digest)

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        """Dry run should not delete anything."""
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()

        store = CASStore(cas_dir)
        digest = store.put(b"dry-run-content", content_type="text/plain")

        # Run GC in dry-run mode
        result = prune_cas_store(sdd_dir, retention_days=0, dry_run=True)

        # Entry should still exist
        assert store.has(digest)
        # Result should show it as deleted candidate
        assert result.deleted_entries == 1
        assert result.deleted_bytes == len(b"dry-run-content")

    def test_receipt_written_after_delete(self, tmp_path: Path) -> None:
        """Receipt is written after successful delete (non-dry-run)."""
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()

        store = CASStore(cas_dir)
        store.put(b"orphan-content", content_type="text/plain")

        # Run GC
        prune_cas_store(sdd_dir, retention_days=0, dry_run=False)

        # Check that a receipt was written (a new entry in CAS)
        entries = store.list_entries()
        receipt_entries = [e for e in entries if e.metadata.get("type") == "cas_prune_receipt"]
        assert len(receipt_entries) >= 1


class TestRunCasGCCli:
    """Tests for run_cas_gc_cli helper."""

    def test_dry_run_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Dry run outputs what would be deleted."""
        from bernstein.core.persistence.cas_gc import run_cas_gc_cli

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()
        CASStore(cas_dir).put(b"orphan", content_type="text/plain")

        success = run_cas_gc_cli(tmp_path, days=0, dry_run=True, yes=True)
        assert success
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out or "Would delete" in captured.out

    def test_negative_days_returns_false(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Negative days returns False."""
        from bernstein.core.persistence.cas_gc import run_cas_gc_cli

        success = run_cas_gc_cli(tmp_path, days=-1, yes=True)
        assert not success
        captured = capsys.readouterr()
        assert "non-negative" in captured.out.lower() or "error" in captured.out.lower()


class TestCASGCCommandIsReachable:
    """The documented command exists on the CLI, not only as a helper function."""

    def test_gc_cas_is_registered(self) -> None:
        """`bernstein gc cas` resolves; docs/architecture/cas-store.md documents it."""
        from click.testing import CliRunner

        from bernstein.cli.main import cli

        result = CliRunner().invoke(cli, ["gc", "cas", "--help"])
        assert result.exit_code == 0, result.output
        assert "No such command" not in result.output

    def test_documented_options_are_accepted(self) -> None:
        """Every flag the architecture doc lists is a real option."""
        from click.testing import CliRunner

        from bernstein.cli.main import cli

        result = CliRunner().invoke(cli, ["gc", "cas", "--help"])
        for flag in ("--days", "--dry-run", "--workdir"):
            assert flag in result.output, f"{flag} is documented but not offered"

    def test_dry_run_reaches_the_store(self, tmp_path: Path) -> None:
        """The command drives the real prune path rather than exiting early."""
        from click.testing import CliRunner

        from bernstein.cli.main import cli

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        cas_dir = sdd_dir / "cas"
        cas_dir.mkdir()
        CASStore(cas_dir).put(b"orphan", content_type="text/plain")

        result = CliRunner().invoke(cli, ["gc", "cas", "--workdir", str(tmp_path), "--days", "0", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Would delete" in result.output or "DRY RUN" in result.output

    def test_missing_sdd_directory_exits_nonzero(self, tmp_path: Path) -> None:
        """A workdir with no .sdd fails loudly instead of reporting success."""
        from click.testing import CliRunner

        from bernstein.cli.main import cli

        result = CliRunner().invoke(cli, ["gc", "cas", "--workdir", str(tmp_path), "--yes"])
        assert result.exit_code != 0
