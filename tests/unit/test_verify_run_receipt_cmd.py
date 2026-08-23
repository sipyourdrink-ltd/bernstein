"""CLI tests for ``bernstein verify run`` / ``bernstein verify receipt`` (#2924).

Also pins the group promotion: ``verify`` became a click group with a
default ``legacy`` subcommand, and every pre-existing flag/positional mode
must keep its exact behaviour and exit codes through the new routing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal

_RUN_ID = "run-cli-fixture"
_HMAC_KEY = b"x" * 32


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a wide terminal so Rich does not truncate asserted output."""
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture(autouse=True)
def _no_ambient_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests choose their key explicitly; ambient env config must not leak in."""
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", raising=False)
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR", raising=False)


def _seed_run(workdir: Path, run_id: str = _RUN_ID) -> None:
    sdd = workdir / ".sdd"
    journal = EventJournal(run_id=run_id, sdd_dir=sdd)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1")
    journal.record("run_completed", run_id=run_id)
    spine = LineageSpine(sdd / "lineage", run_id=run_id, hmac_key=_HMAC_KEY)
    spine.record(
        artifact_path="src/app.py",
        content=b"x",
        actor="backend",
        step_id="T-1",
        model="m",
        timestamp=1234,
    )


def _write_signing_key(path: Path) -> None:
    key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


# ---------------------------------------------------------------------------
# verify run + verify receipt
# ---------------------------------------------------------------------------


def test_verify_run_builds_receipt_then_receipt_verb_verifies_offline(tmp_path: Path) -> None:
    """`verify run` writes the receipt; `verify receipt` checks it -> exit 0.

    The receipt verb runs from an unrelated cwd with no .sdd/ so the check
    provably uses only the file.
    """
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)

    runner = CliRunner()
    built = runner.invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output
    receipt_path = workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"
    assert receipt_path.exists()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with runner.isolated_filesystem(temp_dir=elsewhere):
        verified = runner.invoke(verify_cmd, ["receipt", str(receipt_path)])
    assert verified.exit_code == 0, verified.output
    assert "OK" in verified.output


def test_verify_run_honours_env_configured_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without flags, the run verb falls back to the finalization-hook env key."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)
    monkeypatch.setenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", str(sign_key))

    result = CliRunner().invoke(verify_cmd, ["run", _RUN_ID, "-w", str(workdir)])
    assert result.exit_code == 0, result.output
    assert (workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json").exists()


def test_verify_run_without_signing_key_exits_2(tmp_path: Path) -> None:
    """No key flag and no env config: usage error, receipts are never unsigned."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)

    result = CliRunner().invoke(verify_cmd, ["run", _RUN_ID, "-w", str(workdir)])
    assert result.exit_code == 2
    assert "No signing key configured" in result.output


def test_verify_run_on_empty_run_exits_1(tmp_path: Path) -> None:
    """A run with no journal has no identity to attest -> exit 1."""
    workdir = tmp_path / "proj"
    (workdir / ".sdd").mkdir(parents=True)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)

    result = CliRunner().invoke(
        verify_cmd,
        ["run", "run-none", "-w", str(workdir), "--signing-key-path", str(sign_key)],
    )
    assert result.exit_code == 1
    assert "no journal events" in result.output


def test_receipt_verb_missing_file_exits_1(tmp_path: Path) -> None:
    result = CliRunner().invoke(verify_cmd, ["receipt", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
    assert "Cannot read receipt" in result.output


def test_receipt_verb_malformed_json_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(verify_cmd, ["receipt", str(bad)])
    assert result.exit_code == 1
    assert "MALFORMED" in result.output


def test_receipt_verb_tamper_exits_2_and_names_step(tmp_path: Path) -> None:
    """A mutated journal row exits 2 and the output names the divergent step."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)

    runner = CliRunner()
    built = runner.invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output
    receipt_path = workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"
    doc = json.loads(receipt_path.read_bytes())
    doc["journal"]["events"][1]["task_id"] = "T-FORGED"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(doc), encoding="utf-8")

    result = runner.invoke(verify_cmd, ["receipt", str(tampered)])
    assert result.exit_code == 2
    assert "TAMPER DETECTED" in result.output
    assert "step: 1" in result.output


def test_receipt_verb_labels_integrity_only_vs_provenance(tmp_path: Path) -> None:
    """A pass names its trust level so a TOFU pass cannot be misread.

    Without --public-key the verdict is labelled integrity-only (embedded
    key, trust-on-first-use); with the operator's pinned key it is labelled
    provenance.
    """
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)

    runner = CliRunner()
    built = runner.invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output
    receipt_path = workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"

    unpinned = runner.invoke(verify_cmd, ["receipt", str(receipt_path)])
    assert unpinned.exit_code == 0, unpinned.output
    assert "integrity-only" in unpinned.output
    assert "provenance: pinned key" not in unpinned.output

    right_pub = tmp_path / "right.pub.pem"
    right_key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    right_pub.write_bytes(
        right_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    pinned = runner.invoke(verify_cmd, ["receipt", str(receipt_path), "--public-key", str(right_pub)])
    assert pinned.exit_code == 0, pinned.output
    assert "provenance: pinned key" in pinned.output
    assert "integrity-only" not in pinned.output


def test_receipt_verb_wrong_public_key_exits_2(tmp_path: Path) -> None:
    """A --public-key pin for a different key rejects the receipt."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)

    runner = CliRunner()
    built = runner.invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output
    receipt_path = workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"

    other_pub = tmp_path / "other.pub.pem"
    other_key = Ed25519PrivateKey.from_private_bytes(b"o" * 32)
    other_pub.write_bytes(
        other_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    result = runner.invoke(verify_cmd, ["receipt", str(receipt_path), "--public-key", str(other_pub)])
    assert result.exit_code == 2
    assert "pinned" in result.output


# ---------------------------------------------------------------------------
# --require-provenance / --json (#4208): a script must be able to gate on
# the provenance tier by exit code, or by the machine-readable tier field,
# without parsing the human verdict prose.
# ---------------------------------------------------------------------------


def _built_receipt(tmp_path: Path) -> tuple[Path, Path]:
    """A signed, self-consistent receipt plus the matching pinned pubkey."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _seed_run(workdir)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)

    built = CliRunner().invoke(
        verify_cmd,
        ["run", _RUN_ID, "-w", str(workdir), "--signing-key-path", str(sign_key)],
    )
    assert built.exit_code == 0, built.output
    receipt_path = workdir / ".sdd" / "runs" / _RUN_ID / "run-receipt.json"

    pub_path = tmp_path / "worker.pub.pem"
    signing_key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    pub_path.write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    return receipt_path, pub_path


def test_integrity_only_pass_exits_0_without_require_provenance(tmp_path: Path) -> None:
    """Today's behaviour is preserved by default: no --public-key still exits 0."""
    receipt_path, _pub_path = _built_receipt(tmp_path)

    result = CliRunner().invoke(verify_cmd, ["receipt", str(receipt_path)])

    assert result.exit_code == 0, result.output


def test_integrity_only_pass_under_require_provenance_exits_nonzero(tmp_path: Path) -> None:
    """The gap #4208 reports: a CI gate must be able to refuse an unpinned pass."""
    receipt_path, _pub_path = _built_receipt(tmp_path)

    result = CliRunner().invoke(verify_cmd, ["receipt", str(receipt_path), "--require-provenance"])

    assert result.exit_code == 3, result.output
    assert "integrity-only" in result.output


def test_provenance_pass_under_require_provenance_exits_0(tmp_path: Path) -> None:
    """A pinned, matching key satisfies --require-provenance."""
    receipt_path, pub_path = _built_receipt(tmp_path)

    result = CliRunner().invoke(
        verify_cmd,
        ["receipt", str(receipt_path), "--public-key", str(pub_path), "--require-provenance"],
    )

    assert result.exit_code == 0, result.output


def test_json_tier_field_distinguishes_integrity_only_from_provenance(tmp_path: Path) -> None:
    """A caller reading JSON must not have to parse the verdict sentence."""
    receipt_path, pub_path = _built_receipt(tmp_path)
    runner = CliRunner()

    unpinned = runner.invoke(verify_cmd, ["receipt", str(receipt_path), "--json"])
    pinned = runner.invoke(verify_cmd, ["receipt", str(receipt_path), "--public-key", str(pub_path), "--json"])

    unpinned_payload = json.loads(unpinned.output)
    pinned_payload = json.loads(pinned.output)
    assert unpinned.exit_code == 0, unpinned.output
    assert pinned.exit_code == 0, pinned.output
    assert unpinned_payload["tier"] == "integrity-only"
    assert pinned_payload["tier"] == "provenance"


def test_json_require_provenance_refusal_still_names_the_tier_reached(tmp_path: Path) -> None:
    """The exit-3 refusal is JSON-visible too, not only in the human verdict."""
    receipt_path, _pub_path = _built_receipt(tmp_path)

    result = CliRunner().invoke(
        verify_cmd,
        ["receipt", str(receipt_path), "--require-provenance", "--json"],
    )

    payload = json.loads(result.output)
    assert result.exit_code == 3, result.output
    assert payload["ok"] is True
    assert payload["tier"] == "integrity-only"
    assert payload["require_provenance"] is True
    assert payload["exit_code"] == 3


def test_json_tampered_receipt_reports_null_tier(tmp_path: Path) -> None:
    """A receipt that never verified has no tier to report."""
    receipt_path, _pub_path = _built_receipt(tmp_path)
    doc = json.loads(receipt_path.read_bytes())
    doc["journal"]["events"][1]["task_id"] = "T-FORGED"
    tampered = receipt_path.parent / "tampered.json"
    tampered.write_text(json.dumps(doc), encoding="utf-8")

    result = CliRunner().invoke(verify_cmd, ["receipt", str(tampered), "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 2
    assert payload["tier"] is None


# ---------------------------------------------------------------------------
# Group promotion: legacy modes keep their exact behaviour and exit codes
# ---------------------------------------------------------------------------


def test_legacy_bare_verify_survives_group_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare `bernstein verify` still prints the usage hint and exits 0."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(verify_cmd, [])
    assert result.exit_code == 0
    assert "--wal-integrity" in result.output


def test_legacy_wal_integrity_missing_run_survives_group_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify --wal-integrity <missing>` routes to legacy and exits 1."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(verify_cmd, ["--wal-integrity", "no-such-run"])
    assert result.exit_code == 1
    assert "WAL file not found" in result.output


def test_legacy_wheelhouse_positional_survives_group_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify <path>` (positional, not a subcommand name) routes to legacy."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(verify_cmd, [str(tmp_path / "no-such-wheelhouse")])
    assert result.exit_code == 1
    assert "Wheelhouse not found" in result.output


def test_legacy_memory_audit_survives_group_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify --memory-audit` with no lesson memory still exits 0."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(verify_cmd, ["--memory-audit"])
    assert result.exit_code == 0
    assert "No lesson memory found" in result.output


def test_legacy_expect_baseline_conflict_survives_group_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--expect and --baseline stay mutually exclusive (usage error, exit 2)."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        verify_cmd,
        ["--determinism", "run-a", "--expect", "aa", "--baseline", "run-b"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_legacy_determinism_gate_mismatch_survives_group_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify --determinism <run> --expect <fp>` keeps its 0/2 gate contract."""
    from bernstein.core.wal import ExecutionFingerprint, WALReader, WALWriter

    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    writer = WALWriter(run_id="run-a", sdd_dir=sdd)
    writer.append("tick_start", {"tick": 1}, {}, "actor")
    fingerprint = ExecutionFingerprint.from_wal(WALReader(run_id="run-a", sdd_dir=sdd)).compute()

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    match = runner.invoke(verify_cmd, ["--determinism", "run-a", "--expect", fingerprint])
    assert match.exit_code == 0
    mismatch = runner.invoke(verify_cmd, ["--determinism", "run-a", "--expect", "0" * 64])
    assert mismatch.exit_code == 2


def test_legacy_subcommand_is_explicitly_addressable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify legacy --wal-integrity ...` names the default mode explicitly.

    This is also the documented escape hatch for a wheelhouse directory
    literally named ``run`` or ``receipt``.
    """
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(verify_cmd, ["legacy", "--wal-integrity", "no-such-run"])
    assert result.exit_code == 1
    assert "WAL file not found" in result.output
