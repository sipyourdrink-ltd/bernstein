"""End-to-end CLI tests for ``bernstein payment-mandate`` (issue #2612).

Exercises the whole surface -- issue, show, spend (authorized + refused), verify
-- and the tamper path, so acceptance criteria 1-5 hold from the operator's
command line, not only at the library layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.payment_mandate_cmd import payment_mandate_group
from bernstein.core.payments.enforce import mandates_dir
from bernstein.core.payments.receipt import receipts_dir


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _issue(runner: CliRunner, project: Path, **extra: str) -> str:
    args = [
        "issue",
        "--workdir",
        str(project),
        "--presence-mode",
        "delegated",
        "--max-amount",
        "100.00",
        "--currency",
        "USD",
        "--recipient",
        "vendor:acme",
        "--not-after",
        "2000000000",
        "--issued-at",
        "1900000000",
        "--nonce",
        "n0",
        "--per-tx-cap",
        "25.00",
        "--allowed-category",
        "data",
    ]
    for k, v in extra.items():
        args += [f"--{k}", v]
    res = runner.invoke(payment_mandate_group, args)
    assert res.exit_code == 0, res.output
    # The issued mandate hash is the single stored mandate file's stem.
    stored = list(mandates_dir(project).glob("*.json"))
    assert len(stored) == 1
    return "sha256:" + stored[0].stem


def test_issue_produces_signed_content_addressed_mandate(project: Path) -> None:
    runner = CliRunner()
    mandate_hash = _issue(runner, project)
    assert mandate_hash.startswith("sha256:")
    row = json.loads((mandates_dir(project) / f"{mandate_hash.split(':')[1]}.json").read_text())
    assert row["signature"]
    assert row["max_amount_nanos"] == "100000000000"


def test_show_verifies_signature_offline(project: Path) -> None:
    runner = CliRunner()
    mandate_hash = _issue(runner, project)
    res = runner.invoke(payment_mandate_group, ["show", mandate_hash, "--workdir", str(project)])
    assert res.exit_code == 0, res.output
    assert "OK" in res.output
    assert "vendor:acme" in res.output


def test_spend_in_scope_is_authorized_and_verifies(project: Path) -> None:
    runner = CliRunner()
    mandate_hash = _issue(runner, project)
    spend = runner.invoke(
        payment_mandate_group,
        [
            "spend",
            "--workdir",
            str(project),
            "--mandate",
            mandate_hash,
            "--amount",
            "20.00",
            "--to",
            "vendor:acme",
            "--category",
            "data",
            "--presence-mode",
            "delegated",
            "--now",
            "1900000000",
            "--nonce",
            "r0",
        ],
    )
    assert spend.exit_code == 0, spend.output
    assert "authorized" in spend.output.lower()

    receipt_files = list(receipts_dir(project).glob("*.json"))
    assert len(receipt_files) == 1
    receipt_hash = "sha256:" + receipt_files[0].stem

    verify = runner.invoke(payment_mandate_group, ["verify", "--receipt", receipt_hash, "--workdir", str(project)])
    assert verify.exit_code == 0, verify.output
    assert "OK" in verify.output


def test_spend_out_of_scope_is_refused_with_reason(project: Path) -> None:
    runner = CliRunner()
    mandate_hash = _issue(runner, project)
    spend = runner.invoke(
        payment_mandate_group,
        [
            "spend",
            "--workdir",
            str(project),
            "--mandate",
            mandate_hash,
            "--amount",
            "500.00",
            "--to",
            "vendor:acme",
            "--category",
            "data",
            "--presence-mode",
            "delegated",
            "--now",
            "1900000000",
            "--nonce",
            "r1",
        ],
    )
    # Refused is a non-zero exit but still emits an anchored receipt.
    assert spend.exit_code != 0
    assert "refused" in spend.output.lower()
    assert "over_max_amount" in spend.output
    assert len(list(receipts_dir(project).glob("*.json"))) == 1


def test_verify_detects_tampered_receipt(project: Path) -> None:
    runner = CliRunner()
    mandate_hash = _issue(runner, project)
    runner.invoke(
        payment_mandate_group,
        [
            "spend",
            "--workdir",
            str(project),
            "--mandate",
            mandate_hash,
            "--amount",
            "20.00",
            "--to",
            "vendor:acme",
            "--category",
            "data",
            "--presence-mode",
            "delegated",
            "--now",
            "1900000000",
            "--nonce",
            "r0",
        ],
    )
    receipt_file = next(iter(receipts_dir(project).glob("*.json")))
    receipt_hash = "sha256:" + receipt_file.stem
    row = json.loads(receipt_file.read_text())
    row["amount_nanos"] = "999999999999"
    receipt_file.write_text(json.dumps(row))

    verify = runner.invoke(payment_mandate_group, ["verify", "--receipt", receipt_hash, "--workdir", str(project)])
    assert verify.exit_code != 0
    assert "FAIL" in verify.output or "fail" in verify.output.lower()
