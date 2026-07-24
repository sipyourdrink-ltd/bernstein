"""``bernstein a2a`` CLI: offline receipt verification and publication (#2609)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.a2a_cmd import a2a_group
from bernstein.core.protocols.a2a.publish import verify_publication_record

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _issue_receipt(tmp_path: Path) -> tuple[dict, dict]:
    """Return ``(response_payload, receipt_dict)`` from a real issuer."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.protocols.a2a.receipt import A2AReceiptIssuer
    from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

    key_path = tmp_path / "head.pem"
    key_path.write_bytes(
        Ed25519PrivateKey.from_private_bytes(b"\x03" * 32).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    issuer = A2AReceiptIssuer(
        spine=LineageSpine(tmp_path / "lineage", run_id="a2a-cli-test", hmac_key=b"k" * 32),
        kid="kid-1",
        kms_adapter=FileBasedKMSAdapter(key_path, kid="kid-1"),
    )
    response = {"id": "t-1", "answer": "the answer"}
    receipt = issuer.issue(task_id="t-1", response=response)
    return response, receipt.to_dict()


# ---------------------------------------------------------------------------
# a2a verify --receipt
# ---------------------------------------------------------------------------


def test_verify_accepts_a_valid_receipt(runner: CliRunner, tmp_path: Path) -> None:
    response, receipt = _issue_receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path)],
    )

    assert result.exit_code == 0, result.output
    assert "valid" in result.output.lower()


def test_verify_rejects_a_tampered_answer(runner: CliRunner, tmp_path: Path) -> None:
    """EMPIRICAL: one byte changed in the answer and the CLI exits non-zero."""
    response, receipt = _issue_receipt(tmp_path)
    response["answer"] = "the ansver"

    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path)],
    )

    assert result.exit_code == 1
    assert "content_hash" in result.output


def test_verify_rejects_a_receipt_with_no_signature(runner: CliRunner, tmp_path: Path) -> None:
    """Strip the chain signature and the response is unverifiable."""
    response, receipt = _issue_receipt(tmp_path)
    receipt["head_signature"] = {}

    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path)],
    )

    assert result.exit_code == 1
    assert "head_signature" in result.output


def test_verify_emits_json_when_asked(runner: CliRunner, tmp_path: Path) -> None:
    response, receipt = _issue_receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["entry_hash"] == receipt["entry_hash"]


def test_verify_warns_when_the_key_is_not_pinned(runner: CliRunner, tmp_path: Path) -> None:
    """A default verify trusts the receipt's own key on first use.

    Without ``--trusted-jwk`` the signature is checked against a key the
    issuer itself supplied, so a valid result proves internal consistency,
    not provenance. That distinction must be surfaced, not silent, or an
    operator reads "valid" as "verified against the operator's key".
    """
    response, receipt = _issue_receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path)],
    )

    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "trust-on-first-use" in lowered or "not pinned" in lowered
    assert "--trusted-jwk" in result.output


def test_verify_json_reports_the_key_is_not_pinned(runner: CliRunner, tmp_path: Path) -> None:
    response, receipt = _issue_receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["key_pinned"] is False
    assert payload.get("warning")


def test_verify_does_not_warn_when_the_key_is_pinned(runner: CliRunner, tmp_path: Path) -> None:
    response, receipt = _issue_receipt(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    response_path = tmp_path / "response.json"
    jwk_path = tmp_path / "trusted.jwk"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    response_path.write_text(json.dumps(response), encoding="utf-8")
    # Pin against exactly the key the receipt was signed with.
    jwk_path.write_text(json.dumps(receipt["head_signature"]["public_key_jwk"]), encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        [
            "verify",
            "--receipt",
            str(receipt_path),
            "--response",
            str(response_path),
            "--trusted-jwk",
            str(jwk_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["key_pinned"] is True
    assert "warning" not in payload


def test_verify_rejects_a_malformed_receipt_file(runner: CliRunner, tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("{not json", encoding="utf-8")
    response_path = tmp_path / "response.json"
    response_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(
        a2a_group,
        ["verify", "--receipt", str(receipt_path), "--response", str(response_path)],
    )

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# a2a publish
# ---------------------------------------------------------------------------


def test_publish_writes_verifiable_records(runner: CliRunner, tmp_path: Path) -> None:
    out_dir = tmp_path / "publish"

    result = runner.invoke(
        a2a_group,
        ["publish", "--endpoint", "https://node.example/a2a", "--output-dir", str(out_dir)],
    )

    assert result.exit_code == 0, result.output
    for surface in ("a2a-card", "mcp-registry"):
        path = out_dir / f"{surface}.json"
        assert path.exists(), f"missing {surface} record"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert verify_publication_record(record).ok, record


def test_publish_can_target_a_single_surface(runner: CliRunner, tmp_path: Path) -> None:
    out_dir = tmp_path / "publish"

    result = runner.invoke(
        a2a_group,
        [
            "publish",
            "--endpoint",
            "https://node.example/a2a",
            "--output-dir",
            str(out_dir),
            "--surface",
            "a2a-card",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out_dir / "a2a-card.json").exists()
    assert not (out_dir / "mcp-registry.json").exists()


def test_publish_rejects_an_unknown_surface(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        a2a_group,
        [
            "publish",
            "--endpoint",
            "https://node.example/a2a",
            "--output-dir",
            str(tmp_path / "p"),
            "--surface",
            "does-not-exist",
        ],
    )

    assert result.exit_code != 0


def test_publish_output_is_deterministic(runner: CliRunner, tmp_path: Path) -> None:
    """Republishing an unchanged node must not churn the registry record."""
    args = ["publish", "--endpoint", "https://node.example/a2a", "--card", str(tmp_path / "card.json")]

    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    assert runner.invoke(a2a_group, [*args, "--output-dir", str(first_dir)]).exit_code == 0
    assert runner.invoke(a2a_group, [*args, "--output-dir", str(second_dir)]).exit_code == 0

    for surface in ("a2a-card", "mcp-registry"):
        assert (first_dir / f"{surface}.json").read_text(encoding="utf-8") == (
            second_dir / f"{surface}.json"
        ).read_text(encoding="utf-8")


def test_publish_agntcy_ads_writes_a_verifiable_record(runner: CliRunner, tmp_path: Path) -> None:
    out_dir = tmp_path / "publish"

    result = runner.invoke(
        a2a_group,
        [
            "publish",
            "--endpoint",
            "https://node.example/a2a",
            "--output-dir",
            str(out_dir),
            "--card",
            str(tmp_path / "card.json"),
            "--surface",
            "agntcy-ads",
        ],
    )

    assert result.exit_code == 0, result.output
    ads_path = out_dir / "agntcy-ads.json"
    assert ads_path.exists()
    # ADS is opt-in: the default surfaces are not emitted alongside it.
    assert not (out_dir / "a2a-card.json").exists()
    record = json.loads(ads_path.read_text(encoding="utf-8"))
    assert record["surface"] == "agntcy-ads"
    assert verify_publication_record(record).ok, record


def test_publish_agntcy_ads_is_deterministic(runner: CliRunner, tmp_path: Path) -> None:
    """Republishing an unchanged node rewrites byte-identical ADS bytes."""
    args = [
        "publish",
        "--endpoint",
        "https://node.example/a2a",
        "--card",
        str(tmp_path / "card.json"),
        "--surface",
        "agntcy-ads",
    ]
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    assert runner.invoke(a2a_group, [*args, "--output-dir", str(first_dir)]).exit_code == 0
    assert runner.invoke(a2a_group, [*args, "--output-dir", str(second_dir)]).exit_code == 0

    assert (first_dir / "agntcy-ads.json").read_text(encoding="utf-8") == (second_dir / "agntcy-ads.json").read_text(
        encoding="utf-8"
    )


def test_publish_reuses_a_persisted_card(runner: CliRunner, tmp_path: Path) -> None:
    """The card is minted once and reused, so the node keeps one identity."""
    card_path = tmp_path / "card.json"

    runner.invoke(
        a2a_group,
        ["publish", "--endpoint", "https://e/a2a", "--output-dir", str(tmp_path / "a"), "--card", str(card_path)],
    )
    assert card_path.exists()
    first = card_path.read_text(encoding="utf-8")

    runner.invoke(
        a2a_group,
        ["publish", "--endpoint", "https://e/a2a", "--output-dir", str(tmp_path / "b"), "--card", str(card_path)],
    )

    assert card_path.read_text(encoding="utf-8") == first
