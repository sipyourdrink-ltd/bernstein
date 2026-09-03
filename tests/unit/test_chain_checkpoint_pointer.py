"""The atomically-replaced checkpoint pointer readers consult.

The checkpoints ledger (``checkpoints/checkpoints.jsonl``) is append-only on
purpose: never rewriting it is what makes a crash-torn trailing append safe.
That leaves a reader wanting only the newest pin with no single object to
open - it must read and validate the whole ledger while a writer appends to
it.

The pointer (``checkpoints/latest.json``) is that single object: written once
per recorded checkpoint via temp + fsync + rename, signature-validated on
read, and never trusted beyond what its HMAC proves. These tests pin the
properties an operator depends on, including the one the pointer exists to
close: a ledger truncated back over a published pin no longer verifies clean.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.chain_checkpoint import (
    check_pointer,
    checkpoints_path,
    latest_pointer_path,
    load_checkpoints,
    load_latest_checkpoint,
    record_checkpoint,
)
from bernstein.core.persistence.merkle import compute_seal
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"checkpoint-pointer-test-key-0123456"


def _seed(tmp_path: Path, count: int = 4) -> Path:
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=_KEY)
    for i in range(count):
        log.log("test.event", "tester", "task", f"t-{i}", {"i": i})
    return audit_dir


def _append(audit_dir: Path, marker: str) -> None:
    AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", marker, {})


def _seal_and_pin(audit_dir: Path) -> dict[str, Any]:
    _tree, seal = compute_seal(audit_dir, key=_KEY)
    return record_checkpoint(audit_dir, seal, key=_KEY)


class TestPointerPublication:
    def test_pointer_is_published_without_rewriting_the_append_only_ledger(self, tmp_path: Path) -> None:
        """1. Recording a checkpoint publishes the pointer; the ledger only grows."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)

        pointer = latest_pointer_path(audit_dir)
        assert pointer.is_file()
        ledger_after_first = checkpoints_path(audit_dir).read_bytes()

        _append(audit_dir, "extra")
        _seal_and_pin(audit_dir)

        ledger_after_second = checkpoints_path(audit_dir).read_bytes()
        assert ledger_after_second.startswith(ledger_after_first)
        assert len(ledger_after_second) > len(ledger_after_first)

    def test_pointer_replacement_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        """2. The publish is a rename, not an accumulation of scratch files."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        _append(audit_dir, "extra")
        _seal_and_pin(audit_dir)

        leftovers = [p.name for p in latest_pointer_path(audit_dir).parent.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_pointer_payload_is_the_ledger_head(self, tmp_path: Path) -> None:
        """3. The single mutable object a reader opens names the current pin."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        _append(audit_dir, "extra")
        second = _seal_and_pin(audit_dir)

        assert load_latest_checkpoint(audit_dir, _KEY) == second
        assert load_latest_checkpoint(audit_dir, _KEY) == load_checkpoints(audit_dir, _KEY).last


class TestPointerIsUntrustedInput:
    def test_tampered_pointer_is_not_trusted(self, tmp_path: Path) -> None:
        """4. A pointer edited without the key is rejected, not believed."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)

        pointer = latest_pointer_path(audit_dir)
        doc = json.loads(pointer.read_text())
        doc["payload"]["entry_count"] = 999_999
        pointer.write_text(json.dumps(doc))

        assert load_latest_checkpoint(audit_dir, _KEY) is None
        # A forged pointer degrades to the ledger; it cannot manufacture a
        # verification failure either.
        assert check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY) is None

    def test_unparsable_pointer_degrades_to_the_ledger(self, tmp_path: Path) -> None:
        """5. Garbage in the pointer costs the fast path, never correctness."""
        audit_dir = _seed(tmp_path)
        pinned = _seal_and_pin(audit_dir)
        latest_pointer_path(audit_dir).write_text("{not json")

        assert load_latest_checkpoint(audit_dir, _KEY) is None
        assert load_checkpoints(audit_dir, _KEY).last == pinned
        assert check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY) is None

    def test_deleting_the_pointer_costs_time_never_correctness(self, tmp_path: Path) -> None:
        """6. The pointer is a cache of the ledger head and heals on re-seal."""
        audit_dir = _seed(tmp_path)
        pinned = _seal_and_pin(audit_dir)
        latest_pointer_path(audit_dir).unlink()

        assert load_latest_checkpoint(audit_dir, _KEY) is None
        assert load_checkpoints(audit_dir, _KEY).last == pinned
        assert check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY) is None

        # Re-sealing an unchanged tree appends nothing but republishes the
        # pointer, so a deleted pointer is recoverable without a new pin.
        _seal_and_pin(audit_dir)
        assert load_latest_checkpoint(audit_dir, _KEY) == pinned
        assert len(load_checkpoints(audit_dir, _KEY).checkpoints) == 1


class TestPointerCatchesLedgerTruncation:
    def test_ledger_truncated_over_a_published_pin_is_reported(self, tmp_path: Path) -> None:
        """7. Load-bearing: the pointer names the file when the ledger regresses."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        _append(audit_dir, "extra")
        _seal_and_pin(audit_dir)

        ledger = checkpoints_path(audit_dir)
        first_line = ledger.read_bytes().split(b"\n")[0] + b"\n"
        ledger.write_bytes(first_line)

        problem = check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY)
        assert problem is not None
        assert "latest.json" in problem

    def test_intact_ledger_is_reported_clean(self, tmp_path: Path) -> None:
        """8. Positive control: an unmodified layout produces no pointer verdict."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        _append(audit_dir, "extra")
        _seal_and_pin(audit_dir)

        assert check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY) is None

    def test_pointer_lagging_the_ledger_is_not_a_failure(self, tmp_path: Path) -> None:
        """9. A crash between the append and the publish leaves a stale pointer."""
        audit_dir = _seed(tmp_path)
        first = _seal_and_pin(audit_dir)
        pointer_bytes = latest_pointer_path(audit_dir).read_bytes()

        _append(audit_dir, "extra")
        _seal_and_pin(audit_dir)
        # Rewind the pointer to the older published pin, which is what a crash
        # between the ledger append and the pointer publish leaves behind.
        latest_pointer_path(audit_dir).write_bytes(pointer_bytes)

        assert load_latest_checkpoint(audit_dir, _KEY) == first
        assert check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY) is None


class TestPointerReadTakesNothing:
    def test_pointer_reads_on_a_read_only_directory(self, tmp_path: Path) -> None:
        """10. A verifier holding a read-only copy resolves the pin."""
        audit_dir = _seed(tmp_path)
        pinned = _seal_and_pin(audit_dir)

        checkpoints_dir = latest_pointer_path(audit_dir).parent
        original_mode = checkpoints_dir.stat().st_mode
        os.chmod(checkpoints_dir, 0o500)
        try:
            assert load_latest_checkpoint(audit_dir, _KEY) == pinned
            assert check_pointer(audit_dir, load_checkpoints(audit_dir, _KEY), key=_KEY) is None
        finally:
            os.chmod(checkpoints_dir, original_mode)

    def test_pointer_reads_while_another_process_holds_the_append_lock(self, tmp_path: Path) -> None:
        """11. The reader coordinates with no writer: no lock is taken."""
        audit_dir = _seed(tmp_path)
        pinned = _seal_and_pin(audit_dir)

        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from bernstein.core.security.audit import _chain_append_lock\n"
            "with _chain_append_lock(Path(sys.argv[1])):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(30)\n"
        )
        env = {**os.environ, "UV_NO_SYNC": "1"}
        holder = subprocess.Popen(
            [sys.executable, "-c", script, str(audit_dir)],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "held"
            assert load_latest_checkpoint(audit_dir, _KEY) == pinned
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_concurrent_republish_is_never_observed_partial(self, tmp_path: Path) -> None:
        """12. Readers see the old pointer or the new one, never a torn file."""
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        pointer = latest_pointer_path(audit_dir)

        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from bernstein.core.persistence.chain_checkpoint import (\n"
            "    load_latest_checkpoint, write_latest_pointer,\n"
            ")\n"
            "audit_dir = Path(sys.argv[1])\n"
            "key = bytes.fromhex(sys.argv[2])\n"
            "base = load_latest_checkpoint(audit_dir, key)\n"
            "assert base is not None\n"
            "for i in range(400):\n"
            "    payload = dict(base)\n"
            "    payload['entry_count'] = base['entry_count'] + (i % 2)\n"
            "    write_latest_pointer(audit_dir, payload, key=key)\n"
        )
        env = {**os.environ, "UV_NO_SYNC": "1"}
        writer = subprocess.Popen(
            [sys.executable, "-c", script, str(audit_dir), _KEY.hex()],
            env=env,
        )
        try:
            reads = 0
            torn = 0
            while writer.poll() is None and reads < 4000:
                reads += 1
                if load_latest_checkpoint(audit_dir, _KEY) is None and pointer.exists():
                    torn += 1
            assert torn == 0
        finally:
            writer.wait(timeout=60)
        assert writer.returncode == 0
