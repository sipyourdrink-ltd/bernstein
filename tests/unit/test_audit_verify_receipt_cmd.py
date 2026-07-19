"""``bernstein audit verify --receipt`` offline receipt checking (#2512).

The command is the one-command answer to "the workflow says the run passed but
it did not": it takes the document the automation platform stored, checks it
against the local chain, and on a mismatch reports what the chain recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.trigger_sources.receipt import (
    PROOF_ENVELOPE_KEY,
    admit_trigger,
    emit_status_proof,
    wrap_status_payload,
)

_BODY = json.dumps({"title": "Rotate the deploy key"}).encode()

_EVENT_PAYLOAD = {
    "event_id": "evt-1",
    "kind": "post_task",
    "title": "Task t-42 finished",
    "severity": "error",
    "run_id": "run-9",
    "details": {"status": "failed"},
}


@pytest.fixture()
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the CLI inside an isolated project with its own chain and key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.delenv("BERNSTEIN_AUTOMATION_BRIDGE_ROOT", raising=False)
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _key(workdir: Path) -> bytes:
    return load_or_create_audit_key(workdir / "audit.key")


def _write(workdir: Path, name: str, document: object) -> str:
    path = workdir / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _admit(workdir: Path, *, trigger_id: str = "n8n-1"):
    return admit_trigger(
        root=workdir / ".sdd" / "automation-bridge",
        audit_dir=workdir / ".sdd" / "audit",
        hmac_key=_key(workdir),
        platform="n8n",
        request_path="/webhook",
        trigger_id=trigger_id,
        body=_BODY,
        scope="task:create",
        timestamp=1_700_000_000,
    )


def _status(workdir: Path, *, status: str = "failed"):
    return emit_status_proof(
        root=workdir / ".sdd" / "automation-bridge",
        audit_dir=workdir / ".sdd" / "audit",
        hmac_key=_key(workdir),
        payload=_EVENT_PAYLOAD,
        status=status,
        timestamp=1_700_000_500,
    )


def _run(*args: str):
    return CliRunner().invoke(audit_group, ["verify", *args])


# ---------------------------------------------------------------------------
# Trigger receipts
# ---------------------------------------------------------------------------


def test_verifies_a_stored_trigger_receipt(workdir: Path) -> None:
    """A receipt straight from the webhook response verifies and exits zero."""
    receipt = _admit(workdir).receipt
    result = _run("--receipt", _write(workdir, "receipt.json", receipt.to_dict()))

    assert result.exit_code == 0, result.output
    assert "Trigger Receipt Verified" in result.output


def test_verifies_a_receipt_against_the_original_payload(workdir: Path) -> None:
    """Supplying the original body re-digests it against the receipt."""
    receipt = _admit(workdir).receipt
    (workdir / "body.json").write_bytes(_BODY)
    result = _run(
        "--receipt",
        _write(workdir, "receipt.json", receipt.to_dict()),
        "--payload",
        str(workdir / "body.json"),
    )
    assert result.exit_code == 0, result.output


def test_a_mismatched_payload_fails(workdir: Path) -> None:
    """A body that no longer digests to the receipt's value exits non-zero."""
    receipt = _admit(workdir).receipt
    (workdir / "body.json").write_bytes(_BODY.replace(b"Rotate", b"Delete"))
    result = _run(
        "--receipt",
        _write(workdir, "receipt.json", receipt.to_dict()),
        "--payload",
        str(workdir / "body.json"),
    )
    assert result.exit_code == 1
    assert "payload digest" in result.output


def test_a_tampered_receipt_fails(workdir: Path) -> None:
    """Editing a signed field of the stored receipt exits non-zero."""
    receipt = _admit(workdir).receipt
    document = receipt.to_dict()
    document["scope"] = "admin:all"
    result = _run("--receipt", _write(workdir, "receipt.json", document))

    assert result.exit_code == 1
    assert "Verification Failed" in result.output


def test_a_refusal_receipt_verifies_too(workdir: Path) -> None:
    """The negative path is discoverable: a refusal is a checkable receipt."""
    _admit(workdir, trigger_id="dup")
    refusal = _admit(workdir, trigger_id="dup")
    assert refusal.admitted is False

    result = _run("--receipt", _write(workdir, "refusal.json", refusal.receipt.to_dict()))
    assert result.exit_code == 0, result.output
    assert "refused" in result.output


# ---------------------------------------------------------------------------
# Status callbacks
# ---------------------------------------------------------------------------


def test_verifies_a_delivered_status_callback(workdir: Path) -> None:
    """A callback body as the platform received it verifies and exits zero."""
    envelope = wrap_status_payload(_EVENT_PAYLOAD, _status(workdir))
    result = _run("--receipt", _write(workdir, "callback.json", envelope))

    assert result.exit_code == 0, result.output
    assert "Status Proof Verified" in result.output
    assert "failed" in result.output


def test_a_flipped_status_fails_and_reports_the_recorded_one(workdir: Path) -> None:
    """The dispute case: told 'succeeded', the chain says 'failed'."""
    envelope = wrap_status_payload(_EVENT_PAYLOAD, _status(workdir, status="failed"))
    envelope[PROOF_ENVELOPE_KEY]["status"] = "succeeded"

    result = _run("--receipt", _write(workdir, "callback.json", envelope))
    assert result.exit_code == 1
    assert "Chain-recorded status: failed" in result.output


# ---------------------------------------------------------------------------
# Operator errors
# ---------------------------------------------------------------------------


def test_a_document_that_is_neither_is_refused(workdir: Path) -> None:
    """An unrelated JSON file is reported, not silently accepted."""
    result = _run("--receipt", _write(workdir, "junk.json", {"hello": "world"}))
    assert result.exit_code == 1
    assert "neither a trigger receipt nor a status proof" in result.output


def test_a_non_object_document_is_rejected(workdir: Path) -> None:
    """A JSON array is a usage error, not a verification failure."""
    result = _run("--receipt", _write(workdir, "list.json", [1, 2, 3]))
    assert result.exit_code == 2


def test_payload_without_receipt_is_a_usage_error(workdir: Path) -> None:
    """``--payload`` only means something alongside ``--receipt``."""
    (workdir / "body.json").write_bytes(_BODY)
    result = _run("--payload", str(workdir / "body.json"))
    assert result.exit_code == 2
    assert "--payload requires --receipt" in result.output
