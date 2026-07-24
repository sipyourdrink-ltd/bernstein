"""CLI tests for spine-backed ``bernstein lineage verify`` / ``replay``.

Issue #2292:

* ``lineage verify <run_id>`` walks the spine head hash and prints the
  head as the run's provenance identity.
* Against an empty run it prints a distinct ``no entries`` status and
  exits non-zero (AC5) rather than passing trivially.
* A single-byte mutation of any spine entry is reported as tamper.
* ``lineage replay <run_id>`` lists the chain in append order.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.lineage_cmd import lineage_cmd
from bernstein.core.lineage.spine import LineageSpine

_KEY = b"k" * 32


def _seed(workdir: Path, run_id: str, n: int = 3) -> LineageSpine:
    root = workdir / ".sdd" / "lineage"
    spine = LineageSpine(root, run_id=run_id, hmac_key=_KEY)
    for i in range(n):
        spine.record(
            artifact_path=f"src/{i}.py",
            content=f"c{i}".encode(),
            actor="agent:worker",
            step_id=f"s{i}",
            model="claude",
            timestamp=i,
        )
    return spine


def _run(args: list[str]) -> object:
    # Widen the render width so table cells are not truncated with an ellipsis.
    return CliRunner().invoke(lineage_cmd, args, env={"COLUMNS": "200"})


def test_verify_ok_prints_head(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    spine = _seed(tmp_path, "run-1")
    result = _run(["verify", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert spine.head_hash()[:16] in result.output
    assert "OK" in result.output


def test_verify_empty_run_distinct_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    # Create the .sdd dir but no spine for this run.
    (tmp_path / ".sdd" / "lineage").mkdir(parents=True)
    result = _run(["verify", "no-such-run", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "no entries" in result.output.lower()


def test_verify_detects_tamper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    spine = _seed(tmp_path, "run-1")
    raw = bytearray(spine.spine_path.read_bytes())
    idx = raw.index(b"src/1.py")
    raw[idx + 4] ^= 0x01
    spine.spine_path.write_bytes(bytes(raw))
    result = _run(["verify", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "tamper" in result.output.lower()


def test_replay_lists_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    _seed(tmp_path, "run-1", n=3)
    result = _run(["replay", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "src/0.py" in result.output
    assert "src/2.py" in result.output


def test_replay_empty_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    (tmp_path / ".sdd" / "lineage").mkdir(parents=True)
    result = _run(["replay", "empty", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "no entries" in result.output.lower()


# ---------------------------------------------------------------------------
# Recovery receipt resolution (issue #2557)
# ---------------------------------------------------------------------------


def _seed_receipt(workdir: Path, run_id: str) -> tuple[str, bytes]:
    """Anchor one recovery receipt and return (entry_hash, receipt_bytes)."""
    from bernstein.core.planning.recovery_receipt import RecoveryReceipt, record_receipt_on_spine

    root = workdir / ".sdd" / "lineage"
    spine = LineageSpine(root, run_id=run_id, hmac_key=_KEY)
    receipt = RecoveryReceipt(
        failing_node_id="run-tests",
        recovery_node_id="fix-bugs",
        source_status="failed",
        condition_context={"status": "failed", "result": "3 failing", "output": {}},
    )
    entry_hash = record_receipt_on_spine(receipt, spine=spine, timestamp=0)
    return entry_hash, receipt.canonical_bytes()


def test_verify_receipt_hash_resolves(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    entry_hash, _ = _seed_receipt(tmp_path, "run-1")

    result = _run(["verify", "run-1", "--workdir", str(tmp_path), "--receipt-hash", entry_hash])
    assert result.exit_code == 0, result.output
    assert "receipt resolves" in result.output.lower()


def test_verify_receipt_content_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    entry_hash, receipt_bytes = _seed_receipt(tmp_path, "run-1")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(receipt_bytes)

    result = _run(
        [
            "verify",
            "run-1",
            "--workdir",
            str(tmp_path),
            "--receipt-hash",
            entry_hash,
            "--receipt-file",
            str(receipt_path),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "content matches" in result.output.lower()


def test_verify_receipt_tampered_content_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    entry_hash, _ = _seed_receipt(tmp_path, "run-1")
    forged = tmp_path / "forged.json"
    forged.write_bytes(b'{"tampered": true}')

    result = _run(
        [
            "verify",
            "run-1",
            "--workdir",
            str(tmp_path),
            "--receipt-hash",
            entry_hash,
            "--receipt-file",
            str(forged),
        ]
    )
    assert result.exit_code == 2
    assert "receipt verification failed" in result.output.lower()


def test_verify_receipt_missing_hash_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    _seed_receipt(tmp_path, "run-1")

    result = _run(["verify", "run-1", "--workdir", str(tmp_path), "--receipt-hash", "sha256:deadbeef"])
    assert result.exit_code == 2
    assert "failed" in result.output.lower()


# ---------------------------------------------------------------------------
# Read-only key handling (issue #2639): verify must never mint an HMAC key.
# ---------------------------------------------------------------------------


def test_verify_missing_key_fails_closed_not_tamper(tmp_path: Path, monkeypatch) -> None:
    # No key file exists at the resolved path. A read-only verify must fail
    # closed with a clear key-missing error rather than minting a fresh key --
    # a minted key cannot authenticate the existing chain, so every HMAC tag
    # would fail and the setup error would be misreported as tamper.
    key_path = tmp_path / "state" / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    _seed(tmp_path, "run-1")

    result = _run(["verify", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code == 3, result.output
    assert "tamper" not in result.output.lower()
    assert "key" in result.output.lower()
    # The verify path must never create key material.
    assert not key_path.exists()


def test_verify_key_path_option_reads_named_key(tmp_path: Path, monkeypatch) -> None:
    # The chain was written under a key stored at a non-default location; an
    # auditor points --key-path at it to verify a handed-over evidence package.
    monkeypatch.delenv("BERNSTEIN_AUDIT_KEY_PATH", raising=False)
    key_file = tmp_path / "handover" / "audit.key"
    key_file.parent.mkdir(parents=True)
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    spine = _seed(tmp_path, "run-1")

    result = _run(["verify", "run-1", "--workdir", str(tmp_path), "--key-path", str(key_file)])
    assert result.exit_code == 0, result.output
    assert spine.head_hash()[:16] in result.output
    assert "OK" in result.output


def test_verify_receipt_missing_key_fails_closed(tmp_path: Path, monkeypatch) -> None:
    key_path = tmp_path / "state" / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    entry_hash, _ = _seed_receipt(tmp_path, "run-1")

    result = _run(["verify", "run-1", "--workdir", str(tmp_path), "--receipt-hash", entry_hash])
    assert result.exit_code == 3, result.output
    assert "key" in result.output.lower()
    assert not key_path.exists()
