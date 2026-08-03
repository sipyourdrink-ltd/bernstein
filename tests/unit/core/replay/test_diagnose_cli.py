"""CLI tests for ``bernstein audit diagnose`` / ``audit diagnose verify`` (#2928).

Exercises the Click surface end-to-end against synthetic journals under
``tmp_path``, with an explicit audit HMAC key and Ed25519 signing key so no
test touches the operator's real key material.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.replay.journal import EventJournal

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, Path]:
    """Audit HMAC key (0600) plus an Ed25519 signing keypair."""
    audit_key = tmp_path / "audit.key"
    audit_key.write_bytes(b"cli-test-audit-hmac-key-32-bytes")
    audit_key.chmod(0o600)

    private = Ed25519PrivateKey.generate()
    sign_key = tmp_path / "sign.pem"
    sign_key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_key = tmp_path / "sign-pub.pem"
    public_key.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return {"audit": audit_key, "sign": sign_key, "pub": public_key}


def _receipts_in(sdd: Path) -> list[Path]:
    evidence = sdd / "evidence"
    return sorted(evidence.glob("*.json")) if evidence.is_dir() else []


def _tamper_payload(journal_path: Path, index: int) -> None:
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[index])
    row["step"] = "tampered"
    lines[index] = json.dumps(row, default=str)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _diagnose_args(sdd: Path, keys: dict[str, Path], run_id: str, signal: str) -> list[str]:
    return [
        "diagnose",
        run_id,
        "--signal",
        signal,
        "--sign-key",
        str(keys["sign"]),
        "--sdd-dir",
        str(sdd),
        "--audit-key",
        str(keys["audit"]),
    ]


def test_missing_journal_fails_closed_with_no_receipt(tmp_path: Path, runner: CliRunner, keys: dict[str, Path]) -> None:
    """Strip-the-substrate: no journal -> exit non-zero and zero receipts."""
    sdd = tmp_path / ".sdd"
    result = runner.invoke(audit_group, _diagnose_args(sdd, keys, "run-none", "replay"))

    assert result.exit_code == 2
    assert "no signed per-step record" in result.output
    assert _receipts_in(sdd) == []


def test_blanked_journal_fails_closed_with_no_receipt(tmp_path: Path, runner: CliRunner, keys: dict[str, Path]) -> None:
    """A zero-event journal is refused the same way, with zero receipts."""
    sdd = tmp_path / ".sdd"
    journal_path = sdd / "runs" / "run-blank" / "journal.jsonl"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text("", encoding="utf-8")

    result = runner.invoke(audit_group, _diagnose_args(sdd, keys, "run-blank", "replay"))

    assert result.exit_code == 2
    assert "no signed per-step record" in result.output
    assert _receipts_in(sdd) == []


def test_missing_audit_key_fails_closed_with_no_receipt(
    tmp_path: Path, runner: CliRunner, keys: dict[str, Path]
) -> None:
    """An unavailable audit HMAC key refuses before any receipt is built."""
    sdd = tmp_path / ".sdd"
    EventJournal("run-key", sdd).record("tick", step=0)

    args = _diagnose_args(sdd, keys, "run-key", "replay")
    args[args.index(str(keys["audit"]))] = str(tmp_path / "no-such.key")
    result = runner.invoke(audit_group, args)

    assert result.exit_code == 2
    assert "Audit HMAC key unavailable" in result.output
    assert _receipts_in(sdd) == []


def test_malformed_journal_line_fails_closed_with_no_receipt(
    tmp_path: Path, runner: CliRunner, keys: dict[str, Path]
) -> None:
    """A journal with an unparsable line refuses with exit 2 and zero
    receipts -- diagnose never reads a filtered sequence
    (regression for bot-ack: 3705961185)."""
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-torn", sdd)
    for i in range(3):
        journal.record("tick", step=i)
    with journal.path.open("a", encoding="utf-8") as f:
        f.write("{torn trailing write\n")

    result = runner.invoke(audit_group, _diagnose_args(sdd, keys, "run-torn", "replay"))

    assert result.exit_code == 2
    assert "unparsable line" in result.output
    assert _receipts_in(sdd) == []


def test_unsigned_receipts_are_refused(tmp_path: Path, runner: CliRunner, keys: dict[str, Path]) -> None:
    """Without --sign-key the command refuses rather than emitting unsigned."""
    sdd = tmp_path / ".sdd"
    EventJournal("run-nosig", sdd).record("tick", step=0)

    result = runner.invoke(
        audit_group,
        ["diagnose", "run-nosig", "--signal", "replay", "--sdd-dir", str(sdd), "--audit-key", str(keys["audit"])],
    )

    assert result.exit_code == 2
    assert "--sign-key is required" in result.output
    assert _receipts_in(sdd) == []


def test_intact_chain_diagnoses_clean_without_receipt(tmp_path: Path, runner: CliRunner, keys: dict[str, Path]) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-ok", sdd)
    for i in range(3):
        journal.record("tick", step=i)

    result = runner.invoke(audit_group, _diagnose_args(sdd, keys, "run-ok", "replay"))

    assert result.exit_code == 0
    assert "chain intact" in result.output
    assert _receipts_in(sdd) == []


def test_diagnose_names_culprit_and_receipt_verifies_offline(
    tmp_path: Path, runner: CliRunner, keys: dict[str, Path]
) -> None:
    """Full loop: chain break at step 2 -> receipt written -> verify exit 0."""
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-bad", sdd)
    for i in range(4):
        journal.record("tick", step=i)
    _tamper_payload(journal.path, 2)

    diagnose = runner.invoke(audit_group, [*_diagnose_args(sdd, keys, "run-bad", "replay"), "--json"])
    assert diagnose.exit_code == 1, diagnose.output
    payload = json.loads(diagnose.output)
    assert payload["culprit_index"] == 2
    assert payload["reason_code"] == "chain_break"
    receipts = _receipts_in(sdd)
    assert len(receipts) == 1

    verify = runner.invoke(
        audit_group,
        [
            "diagnose",
            "verify",
            str(receipts[0]),
            "--public-key",
            str(keys["pub"]),
            "--sdd-dir",
            str(sdd),
            "--audit-key",
            str(keys["audit"]),
        ],
    )
    assert verify.exit_code == 0, verify.output
    assert "verified" in verify.output


def test_verify_fails_after_journal_mutation(tmp_path: Path, runner: CliRunner, keys: dict[str, Path]) -> None:
    """A receipt stops verifying once the diagnosed journal is mutated."""
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-mut", sdd)
    for i in range(4):
        journal.record("tick", step=i)
    _tamper_payload(journal.path, 1)

    diagnose = runner.invoke(audit_group, _diagnose_args(sdd, keys, "run-mut", "replay"))
    assert diagnose.exit_code == 1
    receipts = _receipts_in(sdd)
    assert len(receipts) == 1

    _tamper_payload(journal.path, 3)

    verify = runner.invoke(
        audit_group,
        ["diagnose", "verify", str(receipts[0]), "--sdd-dir", str(sdd), "--audit-key", str(keys["audit"])],
    )
    assert verify.exit_code == 1
    assert "FAILED" in verify.output


def test_signal_is_required(tmp_path: Path, runner: CliRunner, keys: dict[str, Path]) -> None:
    sdd = tmp_path / ".sdd"
    result = runner.invoke(
        audit_group,
        ["diagnose", "run-x", "--sign-key", str(keys["sign"]), "--sdd-dir", str(sdd)],
    )
    assert result.exit_code == 2
    assert "--signal is required" in result.output


def test_verify_missing_receipt_exits_2(tmp_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        audit_group, ["diagnose", "verify", str(tmp_path / "nope.json"), "--sdd-dir", str(tmp_path / ".sdd")]
    )
    assert result.exit_code == 2
    assert "Receipt not found" in result.output
