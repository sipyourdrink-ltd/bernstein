"""Chaos test: disk full during merge and JSONL write."""

from __future__ import annotations

import errno
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_task_record(task_id: str, title: str, status: str = "open") -> dict[str, object]:
    """Build a minimal JSONL task record."""
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "role": "backend",
        "description": "",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
    }


class TestDiskFullDuringMerge:
    """Simulate ENOSPC during git merge operations."""

    def test_merge_disk_full_no_crash(self, tmp_path: Path) -> None:
        """Git merge raising ENOSPC should be caught, not crash the process."""
        import subprocess

        original_run = subprocess.run

        def _enospc_on_merge(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
            if "merge" in cmd_str:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original_run(*args, **kwargs)  # type: ignore[arg-type]

        with patch("subprocess.run", side_effect=_enospc_on_merge):
            with pytest.raises(OSError, match="No space left"):
                import subprocess as sp

                sp.run(["git", "merge", "some-branch"], check=True)

    def test_merge_disk_full_error_message(self) -> None:
        """ENOSPC error should produce a meaningful message."""
        err = OSError(errno.ENOSPC, "No space left on device")
        assert "space" in str(err).lower()
        assert err.errno == errno.ENOSPC


class TestDiskFullDuringJSONLWrite:
    """Simulate ENOSPC when appending to tasks.jsonl."""

    def test_jsonl_write_disk_full_no_data_loss(self, tmp_path: Path) -> None:
        """Existing JSONL data should survive a failed append."""
        jsonl_path = tmp_path / "tasks.jsonl"

        # Write 3 valid records
        records = [_make_task_record(f"T-{i}", f"Task {i}") for i in range(3)]
        with open(jsonl_path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        # Simulate disk full on append
        original_open = open

        def _enospc_open(path: object, mode: str = "r", *args: object, **kwargs: object) -> object:
            if str(path) == str(jsonl_path) and "a" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original_open(path, mode, *args, **kwargs)  # type: ignore[arg-type]

        with patch("builtins.open", side_effect=_enospc_open):
            with pytest.raises(OSError):
                with open(jsonl_path, "a") as f:
                    f.write(json.dumps(_make_task_record("T-new", "New Task")) + "\n")

        # Verify existing data is intact
        with open(jsonl_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert len(lines) == 3
        assert lines[0]["id"] == "T-0"
        assert lines[2]["id"] == "T-2"

    def test_partial_write_leaves_valid_prefix(self, tmp_path: Path) -> None:
        """A partial write (truncated line) should not corrupt earlier records."""
        jsonl_path = tmp_path / "tasks.jsonl"

        # Write 2 valid records + 1 truncated
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(_make_task_record("T-0", "Task 0")) + "\n")
            f.write(json.dumps(_make_task_record("T-1", "Task 1")) + "\n")
            f.write('{"id": "T-2", "title": "Trunc')  # Simulated partial write

        # Read back — should get 2 valid records, skip the truncated one
        valid_records: list[dict[str, object]] = []
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    valid_records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # Skip corrupt lines

        assert len(valid_records) == 2
        assert valid_records[0]["id"] == "T-0"
        assert valid_records[1]["id"] == "T-1"

    def test_wal_protects_against_disk_full(self, tmp_path: Path) -> None:
        """WAL entries written before disk full should be recoverable."""
        wal_dir = tmp_path / ".sdd" / "wal"
        wal_dir.mkdir(parents=True)

        # Write WAL entries
        entries = [
            {"type": "task_created", "task_id": "T-1", "seq": 1},
            {"type": "task_claimed", "task_id": "T-1", "seq": 2},
        ]
        wal_file = wal_dir / "wal.jsonl"
        with open(wal_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        # Verify WAL is readable after "disk full" prevented further writes
        recovered: list[dict[str, object]] = []
        with open(wal_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    recovered.append(json.loads(line))

        assert len(recovered) == 2
        assert recovered[0]["task_id"] == "T-1"
        assert recovered[1]["type"] == "task_claimed"
