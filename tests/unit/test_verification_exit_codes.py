"""Unit tests pinning the exit code contracts and verdict strings for verification commands (#4206).

Asserts that changing any exit code or verdict string across the four verification
surfaces breaks CI, preventing undocumented contract drift for operators.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.cli.advanced_cmd import replay_cmd
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.cli.commands.lineage_verify_cmd import lineage_verify_cmd
from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit import AuditLog

_HMAC_KEY = b"k" * 32
_RUN_ID = "run-exit-code-fixture"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force wide COLUMNS to avoid Rich output wrapping in asserted strings."""
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture()
def seeded_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepare a project workdir with a valid journal, lineage spine, and audit chain."""
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)

    # 1. Event Journal
    journal = EventJournal(run_id=_RUN_ID, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=_RUN_ID)
    journal.record("task_claimed", task_id="T-1")
    journal.record("run_completed", run_id=_RUN_ID)

    # 2. Lineage Spine
    spine = LineageSpine(sdd_dir / "lineage", run_id=_RUN_ID, hmac_key=_HMAC_KEY)
    spine.record(
        artifact_path="src/main.py",
        content=b"print('hello')\n",
        actor="backend",
        step_id="T-1",
        model="sonnet",
        timestamp=1,
    )

    # 3. Audit Chain
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_HMAC_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))

    audit_log = AuditLog(sdd_dir / "audit", key=_HMAC_KEY)
    audit_log.log("system_init", actor="test", resource_type="system", resource_id="node-1")

    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_signing_key(path: Path) -> bytes:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    return pem


# ---------------------------------------------------------------------------
# 1. replay <run> --verify
# ---------------------------------------------------------------------------


def test_replay_verify_exit_code_pass(seeded_workdir: Path) -> None:
    """replay <run> --verify returns exit code 0 on an intact journal."""
    sdd_dir = seeded_workdir / ".sdd"
    result = CliRunner().invoke(replay_cmd, [_RUN_ID, "--sdd-dir", str(sdd_dir), "--verify"])
    assert result.exit_code == 0
    assert "chain intact" in result.output.lower()


def test_replay_verify_exit_code_divergence_failure(seeded_workdir: Path) -> None:
    """replay <run> --verify returns exit code 1 on a tampered journal."""
    sdd_dir = seeded_workdir / ".sdd"
    journal_path = sdd_dir / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text().splitlines()
    tampered_row = json.loads(lines[1])
    tampered_row["task_id"] = "T-MUTATED"
    lines[1] = json.dumps(tampered_row)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = CliRunner().invoke(replay_cmd, [_RUN_ID, "--sdd-dir", str(sdd_dir), "--verify"])
    assert result.exit_code == 1


def test_replay_verify_exit_code_missing_journal(seeded_workdir: Path) -> None:
    """replay <run> --verify returns exit code 1 when the journal is missing."""
    sdd_dir = seeded_workdir / ".sdd"
    result = CliRunner().invoke(replay_cmd, ["nonexistent-run", "--sdd-dir", str(sdd_dir), "--verify"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 2. verify receipt <path>
# ---------------------------------------------------------------------------


def test_verify_receipt_exit_code_pass(seeded_workdir: Path) -> None:
    """verify receipt returns exit code 0 and OK status on a valid receipt."""
    sign_key = seeded_workdir / "sign.pem"
    _write_signing_key(sign_key)

    runner = CliRunner()
    built = runner.invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(seeded_workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output

    receipt_file = seeded_workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"
    result = runner.invoke(verify_cmd, ["receipt", str(receipt_file)])
    assert result.exit_code == 0
    assert "OK (integrity-only: embedded key)" in result.output


def test_verify_receipt_exit_code_malformed(seeded_workdir: Path) -> None:
    """verify receipt returns exit code 1 on malformed JSON input."""
    bad_file = seeded_workdir / "bad.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    result = CliRunner().invoke(verify_cmd, ["receipt", str(bad_file)])
    assert result.exit_code == 1
    assert "MALFORMED" in result.output


def test_verify_receipt_exit_code_tamper(seeded_workdir: Path) -> None:
    """verify receipt returns exit code 2 and TAMPER DETECTED on a modified receipt."""
    sign_key = seeded_workdir / "sign.pem"
    _write_signing_key(sign_key)

    runner = CliRunner()
    built = runner.invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(seeded_workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output

    receipt_file = seeded_workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"
    data = json.loads(receipt_file.read_text(encoding="utf-8"))
    data["journal"]["events"][0]["event_type"] = "tampered_event"
    tampered_file = seeded_workdir / "tampered-receipt.json"
    tampered_file.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(verify_cmd, ["receipt", str(tampered_file)])
    assert result.exit_code == 2
    assert "TAMPER DETECTED" in result.output


# ---------------------------------------------------------------------------
# 3. lineage verify <run>
# ---------------------------------------------------------------------------


def test_lineage_verify_exit_code_pass(seeded_workdir: Path) -> None:
    """lineage verify returns exit code 0 and OK status on an intact spine."""
    result = CliRunner().invoke(
        lineage_verify_cmd,
        [_RUN_ID, "--workdir", str(seeded_workdir), "--key-path", str(seeded_workdir / "audit.key")],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_lineage_verify_exit_code_no_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """lineage verify returns exit code 1 and NO ENTRIES on an empty run."""
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_HMAC_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))

    result = CliRunner().invoke(
        lineage_verify_cmd,
        ["empty-run", "--workdir", str(tmp_path), "--key-path", str(key_file)],
    )
    assert result.exit_code == 1
    assert "NO ENTRIES" in result.output


def test_lineage_verify_exit_code_tamper(seeded_workdir: Path) -> None:
    """lineage verify returns exit code 2 and TAMPER DETECTED on a corrupted spine entry."""
    spine_file = seeded_workdir / ".sdd" / "lineage" / _RUN_ID / "spine.jsonl"
    lines = spine_file.read_text().splitlines()
    tampered_entry = json.loads(lines[0])
    tampered_entry["actor"] = "attacker"
    lines[0] = json.dumps(tampered_entry)
    spine_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = CliRunner().invoke(
        lineage_verify_cmd,
        [_RUN_ID, "--workdir", str(seeded_workdir), "--key-path", str(seeded_workdir / "audit.key")],
    )
    assert result.exit_code == 2
    assert "TAMPER DETECTED" in result.output


def test_lineage_verify_exit_code_key_missing(seeded_workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """lineage verify returns exit code 3 and CANNOT VERIFY when the key file is missing."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(seeded_workdir / "nonexistent.key"))
    result = CliRunner().invoke(
        lineage_verify_cmd,
        [_RUN_ID, "--workdir", str(seeded_workdir)],
    )
    assert result.exit_code == 3
    assert "CANNOT VERIFY" in result.output


# ---------------------------------------------------------------------------
# 4. audit verify
# ---------------------------------------------------------------------------


def test_audit_verify_exit_code_pass(seeded_workdir: Path) -> None:
    """audit verify returns exit code 0 when all pillars pass after seal."""
    runner = CliRunner()
    seal_res = runner.invoke(audit_group, ["seal"])
    assert seal_res.exit_code == 0, seal_res.output

    result = runner.invoke(audit_group, ["verify"])
    assert result.exit_code == 0, result.output


def test_audit_verify_exit_code_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit verify returns exit code 1 when the audit directory is missing."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(audit_group, ["verify"])
    assert result.exit_code == 1
    assert "Audit directory not found" in result.output


def test_audit_verify_exit_code_usage_error(seeded_workdir: Path) -> None:
    """audit verify returns exit code 2 when flags are used incorrectly (e.g. --payload without --receipt)."""
    payload_file = seeded_workdir / "payload.json"
    payload_file.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify", "--payload", str(payload_file)])
    assert result.exit_code == 2
    assert "--payload requires --receipt" in result.output
