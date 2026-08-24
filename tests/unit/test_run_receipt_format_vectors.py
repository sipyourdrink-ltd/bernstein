"""Committed run-receipt test vectors are exercised by CI (issue #4204).

``tests/fixtures/receipt-vectors/`` carries a signed run receipt (built
from a deterministic Ed25519 key over a hermetic 3-event / 2-spine run)
and a tampered copy of it. These tests run the exact offline verifier and
the ``bernstein verify receipt`` CLI against those committed files on
every push, so the published evidence cannot rot into a decorative file:
a receipt that stops verifying - or a tampered copy that still looks
official - fails the build. The negative is demonstrated against a real
mutation, not assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.replay.run_receipt import verify_run_receipt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "receipt-vectors"
_VALID = _VECTORS / "valid-run-receipt.json"
_TAMPERED = _VECTORS / "tampered-run-receipt.json"
_PUBKEY = _VECTORS / "valid-run-receipt-key.pem"

_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "receipt_type",
    "run_id",
    "subject",
    "journal",
    "spine",
    "signing",
)


def test_valid_vector_verifies_offline() -> None:
    """The committed valid receipt must verify from its bytes alone."""
    result = verify_run_receipt(_VALID.read_bytes())
    assert result.ok is True
    assert result.status == "ok"


def test_tampered_vector_is_detected() -> None:
    """The committed tampered copy must fail closed with a tamper verdict."""
    result = verify_run_receipt(_TAMPERED.read_bytes())
    assert result.ok is False
    assert result.status == "tampered"


def test_malformed_input_is_rejected(tmp_path: Path) -> None:
    """A file that is not a receipt at all must be reported malformed."""
    malformed = tmp_path / "not-a-receipt.json"
    malformed.write_text('{"this": "is not a run receipt"}', encoding="utf-8")
    result = verify_run_receipt(malformed.read_bytes())
    assert result.status == "malformed"


def test_valid_vector_verifies_with_pinned_public_key() -> None:
    """Pinning the committed public key reaches the provenance tier."""
    result = verify_run_receipt(_VALID.read_bytes(), public_key_pem=_PUBKEY.read_bytes())
    assert result.ok is True
    assert result.status == "ok"


def test_cli_exit_codes(tmp_path: Path) -> None:
    """``bernstein verify receipt`` maps verdicts to exit codes 0/1/2."""
    malformed = tmp_path / "not-a-receipt.json"
    malformed.write_text('{"this": "is not a run receipt"}', encoding="utf-8")

    ok = CliRunner().invoke(verify_cmd, ["receipt", str(_VALID)])
    assert ok.exit_code == 0, ok.output

    tampered = CliRunner().invoke(verify_cmd, ["receipt", str(_TAMPERED)])
    assert tampered.exit_code == 2, tampered.output
    assert "TAMPER DETECTED" in tampered.output

    bad = CliRunner().invoke(verify_cmd, ["receipt", str(malformed)])
    assert bad.exit_code == 1, bad.output
    assert "MALFORMED" in bad.output


def test_valid_vector_has_all_required_top_level_fields() -> None:
    """The committed valid receipt carries every required top-level field."""
    doc = json.loads(_VALID.read_text(encoding="utf-8"))
    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        assert field in doc, f"missing required top-level field: {field}"
