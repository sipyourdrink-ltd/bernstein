"""Tests for CAS garbage collection (bernstein.core.persistence.cas_gc)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.persistence import cas_gc as cas_gc_mod
from bernstein.core.persistence.cas_gc import (
    RootSet,
    _extract_digests_from_obj,
    _scan_audit_seals_for_digests,
    _scan_backlog_for_digests,
    _scan_lineage_for_digests,
    _scan_wal_for_digests,
    collect_referenced_digests,
    collect_root_set,
    prune_cas_store,
)
from bernstein.core.persistence.cas_store import CASStore


def _age_entry(cas_dir: Path, digest: str, *, days: int) -> None:
    """Backdate one CAS entry by rewriting the ``created_at`` in its metadata.

    Retention is decided on that field, so this is the only way to make an
    entry old. Passing ``retention_days=0`` instead sets the cutoff to
    ``time.time()`` and leaves the comparison against an entry written
    microseconds earlier to the clock's resolution, which is 15ms on
    Windows and makes the outcome a coin flip.
    """
    meta_path = cas_dir / digest[:2] / f"{digest}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created_at"] = time.time() - (days * 86400)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


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


class TestLineageScannerToleratesOddLines:
    """One malformed spine line must not take the whole root out of the mark."""

    def test_a_non_string_content_hash_is_skipped(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        spine = sdd / "lineage" / "run-1"
        spine.mkdir(parents=True)
        good = "sha256:" + ("a" * 64)
        spine.joinpath("spine.jsonl").write_text(
            json.dumps({"content_hash": 12345}) + "\n" + json.dumps({"content_hash": good}) + "\n",
            encoding="utf-8",
        )
        assert _scan_lineage_for_digests(sdd) == {"a" * 64}

    def test_a_line_that_is_not_an_object_is_skipped(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        spine = sdd / "lineage" / "run-1"
        spine.mkdir(parents=True)
        good = "sha256:" + ("b" * 64)
        spine.joinpath("spine.jsonl").write_text(
            "[1, 2, 3]\n" + json.dumps({"content_hash": good}) + "\n",
            encoding="utf-8",
        )
        assert _scan_lineage_for_digests(sdd) == {"b" * 64}

    def test_an_odd_line_does_not_make_the_root_unreadable(self, tmp_path: Path) -> None:
        """The mark phase stays complete, so the sweep is not blocked either."""
        sdd = tmp_path / ".sdd"
        spine = sdd / "lineage" / "run-1"
        spine.mkdir(parents=True)
        spine.joinpath("spine.jsonl").write_text(json.dumps({"content_hash": None}) + "\n", encoding="utf-8")
        assert collect_root_set(sdd).complete


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

        _age_entry(cas_dir, old_digest, days=31)

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
        _age_entry(cas_dir, digest, days=31)

        # Run GC in dry-run mode
        result = prune_cas_store(sdd_dir, retention_days=30, dry_run=True)

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
        digest = store.put(b"orphan-content", content_type="text/plain")
        _age_entry(cas_dir, digest, days=31)

        # Run GC
        prune_cas_store(sdd_dir, retention_days=30, dry_run=False)

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

    def test_a_partial_mark_exits_nonzero_and_says_why(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator running `bernstein gc cas` must hear about a refusal."""
        from bernstein.core.persistence.cas_gc import run_cas_gc_cli

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        (sdd_dir / "cas").mkdir()
        CASStore(sdd_dir / "cas").put(b"orphan", content_type="text/plain")

        def boom(_sdd: Path) -> set[str]:
            raise OSError("unreadable")

        monkeypatch.setattr(cas_gc_mod, "_scan_wal_for_digests", boom)
        success = run_cas_gc_cli(tmp_path, days=30, yes=True)

        assert not success
        assert "refusing to delete" in capsys.readouterr().out

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


class TestMarkPhaseCompleteness:
    """A sweep is only as safe as its mark."""

    @staticmethod
    def _store_one_old_unreferenced_blob(tmp_path: Path) -> tuple[CASStore, str]:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        store = CASStore(sdd / "cas")
        digest = store.put(b"payload", content_type="text/plain")
        _age_entry(sdd / "cas", digest, days=120)
        return store, digest

    def test_a_readable_root_set_is_complete(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        root_set = collect_root_set(sdd)
        assert root_set.complete
        assert root_set.unreadable_roots == ()

    def test_a_failing_scanner_is_named_not_only_logged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        def boom(_sdd: Path) -> set[str]:
            raise OSError("lineage directory is unreadable")

        monkeypatch.setattr(cas_gc_mod, "_scan_lineage_for_digests", boom)
        root_set = collect_root_set(sdd)
        assert not root_set.complete
        assert root_set.unreadable_roots == ("Lineage",)

    def test_a_partial_mark_deletes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The blob's only reference may live in the root that did not open.

        Before this guard the scanner's failure was logged and the sweep ran
        on the digests it did have, so an old blob referenced only from the
        unreadable root was deleted as unreferenced.
        """
        store, digest = self._store_one_old_unreferenced_blob(tmp_path)

        def boom(_sdd: Path) -> set[str]:
            raise OSError("lineage directory is unreadable")

        monkeypatch.setattr(cas_gc_mod, "_scan_lineage_for_digests", boom)
        result = prune_cas_store(tmp_path / ".sdd", retention_days=30)

        assert result.unreadable_roots == ("Lineage",)
        assert store.get(digest) is not None, "a blob was deleted on a partial mark"
        assert any("refusing to delete" in error for error in result.errors)

    def test_a_partial_mark_still_reports_what_it_would_have_considered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counts stay informative, the way they do for a dry run."""
        self._store_one_old_unreferenced_blob(tmp_path)

        def boom(_sdd: Path) -> set[str]:
            raise OSError("unreadable")

        monkeypatch.setattr(cas_gc_mod, "_scan_wal_for_digests", boom)
        result = prune_cas_store(tmp_path / ".sdd", retention_days=30)
        assert result.scanned_entries == 1
        assert result.deleted_entries == 1

    def test_a_complete_mark_still_deletes(self, tmp_path: Path) -> None:
        """The guard must not stop an ordinary sweep."""
        store, digest = self._store_one_old_unreferenced_blob(tmp_path)
        result = prune_cas_store(tmp_path / ".sdd", retention_days=30)
        assert result.unreadable_roots == ()
        assert result.errors == []
        assert result.deleted_entries == 1
        assert store.get(digest) is None

    def test_a_partial_mark_writes_no_receipt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A receipt always attests a sweep decided against every root."""
        store, _digest = self._store_one_old_unreferenced_blob(tmp_path)

        def boom(_sdd: Path) -> set[str]:
            raise OSError("unreadable")

        monkeypatch.setattr(cas_gc_mod, "_scan_backlog_for_digests", boom)
        prune_cas_store(tmp_path / ".sdd", retention_days=30)
        receipts = [e for e in store.list_entries() if e.metadata.get("type") == "cas_prune_receipt"]
        assert receipts == []


class TestAnUnreadableFileMakesTheMarkIncomplete:
    """The trigger that actually happens, not just a scanner raising outright.

    Every scanner catches per-file OSError internally and carries on. Those
    failures never reached ``collect_root_set``'s broad except, so a
    permissions problem or a transient I/O error on one spine file left
    ``complete`` True and destroyed live data silently.
    """

    @staticmethod
    def _unreadable_lineage_spine(sdd: Path) -> Path:
        spine = sdd / "lineage" / "run-1"
        spine.mkdir(parents=True)
        path = spine / "spine.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        return path

    def test_a_per_file_oserror_is_reported_not_only_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        self._unreadable_lineage_spine(sdd)

        real_open = Path.open

        def refuse(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "spine.jsonl":
                raise PermissionError("permission denied")
            return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", refuse)
        root_set = collect_root_set(sdd)

        assert not root_set.complete
        assert any("Lineage" in entry for entry in root_set.unreadable_roots)

    def test_a_blob_referenced_from_an_unreadable_root_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: one unreadable file must not cost a live blob."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        store = CASStore(sdd / "cas")
        digest = store.put(b"the only copy", content_type="text/plain")
        _age_entry(sdd / "cas", digest, days=120)

        spine = sdd / "lineage" / "run-1"
        spine.mkdir(parents=True)
        spine.joinpath("spine.jsonl").write_text(
            json.dumps({"content_hash": "sha256:" + digest}) + "\n", encoding="utf-8"
        )

        real_open = Path.open

        def refuse(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "spine.jsonl":
                raise PermissionError("permission denied")
            return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", refuse)
        result = prune_cas_store(sdd, retention_days=30)
        monkeypatch.undo()

        assert result.unreadable_roots, "the mark reported itself complete"
        assert store.has(digest), "a live blob was deleted on a partial mark"

    def test_the_cli_does_not_claim_a_deletion_it_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The headline line is what a log scraper reads."""
        from bernstein.core.persistence.cas_gc import run_cas_gc_cli

        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        store = CASStore(sdd / "cas")
        digest = store.put(b"orphan", content_type="text/plain")
        _age_entry(sdd / "cas", digest, days=120)

        def boom(_sdd: Path, **_kwargs: object) -> set[str]:
            raise OSError("unreadable")

        monkeypatch.setattr(cas_gc_mod, "_scan_wal_for_digests", boom)
        success = run_cas_gc_cli(tmp_path, days=30, yes=True)
        out = capsys.readouterr().out

        assert not success
        assert "CAS GC refused" in out
        assert "CAS GC complete" not in out


class TestRootSetHash:
    """The receipt's root_set_hash says which reachable set justified a sweep."""

    def test_the_hash_does_not_depend_on_discovery_order(self) -> None:
        digests = {"a" * 64, "b" * 64, "c" * 64}
        assert RootSet(frozenset(digests)).content_hash() == RootSet(frozenset(reversed(list(digests)))).content_hash()

    def test_two_different_root_sets_hash_differently(self) -> None:
        assert RootSet(frozenset({"a" * 64})).content_hash() != RootSet(frozenset({"b" * 64})).content_hash()

    def test_the_empty_root_set_has_a_hash(self) -> None:
        assert RootSet(frozenset()).content_hash().startswith("sha256:")

    def test_the_receipt_carries_the_root_set_hash(self, tmp_path: Path) -> None:
        """It was ``null`` in every receipt ever written before this change."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        store = CASStore(sdd / "cas")
        digest = store.put(b"payload", content_type="text/plain")
        _age_entry(sdd / "cas", digest, days=120)

        result = prune_cas_store(sdd, retention_days=30)
        assert result.deleted_entries == 1

        receipts = [e for e in store.list_entries() if e.metadata.get("type") == "cas_prune_receipt"]
        assert len(receipts) == 1
        body = json.loads(store.get(receipts[0].digest) or b"{}")
        assert body["version"] == 2
        assert body["root_set_hash"] == result.root_set_hash
        assert body["root_set_hash"].startswith("sha256:")


class TestCollectReferencedDigestsStillWorks:
    def test_the_digests_only_view_matches_the_root_set(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        assert collect_referenced_digests(sdd) == set(collect_root_set(sdd).digests)
