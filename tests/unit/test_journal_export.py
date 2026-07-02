"""Tests for ``bernstein.core.persistence.journal_export`` (#1799).

A portable receipt is the chain slice + the head hash + any referenced
CAS blobs (so a downstream verifier doesn't need the originating host).
The receipt round-trips:

1. ``export_receipt`` writes a tarball / directory bundle.
2. ``verify_receipt`` validates the bundle offline using only the public
   key (when signed) and the bundled head hash.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from bernstein.core.persistence.journal import Journal, JournalReader
from bernstein.core.persistence.journal_export import (
    ReceiptError,
    export_receipt,
    verify_receipt,
)


def _populate(agent_dir: Path, n_steps: int = 3) -> JournalReader:
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
    return JournalReader(agent_dir)


class TestExport:
    def test_export_includes_head_hash_and_steps(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        reader = _populate(agent_dir, n_steps=3)
        receipt_path = tmp_path / "receipt.tar"

        result = export_receipt(
            agent_dir,
            receipt_path,
            agent_id="agent-1",
        )

        assert receipt_path.exists()
        assert result.head_hash == reader.head().step_hash  # type: ignore[union-attr]
        assert result.steps == 3

    def test_export_is_offline_verifiable_with_head_hash(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        reader = _populate(agent_dir, n_steps=3)
        receipt_path = tmp_path / "receipt.tar"
        head = reader.head().step_hash  # type: ignore[union-attr]
        export_receipt(agent_dir, receipt_path, agent_id="agent-1")

        # Move the original journal away to prove verify reads only the receipt.
        scrubbed = tmp_path / "scrubbed"
        agent_dir.rename(scrubbed)
        assert not agent_dir.exists()

        result = verify_receipt(receipt_path, expected_head=head)
        assert result.ok, result.errors
        assert result.head_hash == head
        assert result.steps == 3

    def test_tampered_receipt_fails_verification(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        reader = _populate(agent_dir, n_steps=2)
        receipt_path = tmp_path / "receipt.tar"
        head = reader.head().step_hash  # type: ignore[union-attr]
        export_receipt(agent_dir, receipt_path, agent_id="agent-1")

        # Tamper: extract, mutate a row, re-archive.
        extract_dir = tmp_path / "tamper"
        with tarfile.open(receipt_path) as tar:
            tar.extractall(extract_dir, filter="data")
        journal_files = list(extract_dir.rglob("*.jsonl"))
        assert journal_files
        lines = journal_files[0].read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["prompt"] = "EVIL"
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        journal_files[0].write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Repack with the original member layout so verifier still finds the journal.
        repacked = tmp_path / "tampered.tar"
        with tarfile.open(repacked, "w") as tar:
            for item in extract_dir.rglob("*"):
                tar.add(item, arcname=item.relative_to(extract_dir))

        result = verify_receipt(repacked, expected_head=head)
        assert not result.ok

    def test_missing_head_raises(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        _populate(agent_dir, n_steps=1)
        receipt_path = tmp_path / "receipt.tar"
        export_receipt(agent_dir, receipt_path, agent_id="agent-1")

        # Verifier insists on an expected head; supplying the wrong one fails.
        result = verify_receipt(receipt_path, expected_head="f" * 64)
        assert not result.ok

    def test_export_refuses_when_journal_empty(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        receipt_path = tmp_path / "receipt.tar"
        with pytest.raises(ReceiptError):
            export_receipt(agent_dir, receipt_path, agent_id="agent-1")


class TestExportEffortRoundTrip:
    """An effort-bearing journal must stay offline-verifiable end to end.

    Regression for the export/publish recompute paths that folded every
    hashed field except ``effort``: an effort-bearing row's bundled step_hash
    included effort while the offline walker recomputed without it, so
    ``verify_receipt`` failed on a chain the local reader accepted.
    """

    def test_effort_bearing_journal_roundtrips_offline(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-effort"
        journal = Journal.open(agent_dir)
        journal.append(input_hash="a0", model="m1", prompt="p0", effort="high")
        journal.append(input_hash="a1", model="m1", prompt="p1", effort="low")
        journal.close()

        reader = JournalReader(agent_dir)
        # Local reader must accept the effort-bearing chain.
        assert reader.verify().ok
        head = reader.head().step_hash  # type: ignore[union-attr]

        receipt_path = tmp_path / "receipt.tar"
        result = export_receipt(agent_dir, receipt_path, agent_id="agent-effort")
        assert result.head_hash == head

        # OFFLINE third-party verify must reproduce the same head_hash from the
        # bundled bytes, which only holds if the walker folds effort into the
        # recomputed step hash.
        verified = verify_receipt(receipt_path, expected_head=head)
        assert verified.ok, f"offline verify must pass; errors={verified.errors}"
        assert verified.steps == 2
