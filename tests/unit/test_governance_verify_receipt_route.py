"""``POST /governance/verify-receipt`` answers about the file, not about itself (#5067).

The route owns no verification: it hands the uploaded bytes to
:func:`bernstein.core.security.governance_receipt_verdict.receipt_verdict_json`
and returns those bytes verbatim, so what an operator reads on the screen is
what ``bernstein verify receipt --json`` reads from the same file offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.routes.governance import router as governance_router
from bernstein.core.security.governance_receipt_verdict import receipt_verdict_json

_VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "receipt-vectors"
_VALID = _VECTORS / "valid-run-receipt.json"
_TAMPERED = _VECTORS / "tampered-run-receipt.json"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(governance_router)
    return TestClient(app)


def test_route_returns_the_verdict_bytes_verbatim(client: TestClient) -> None:
    """One serialiser: the response body is the projection's own bytes."""
    payload = _VALID.read_bytes()

    response = client.post("/governance/verify-receipt", content=payload)

    assert response.status_code == 200
    assert response.text == receipt_verdict_json(payload)


def test_route_verdict_matches_the_offline_cli_for_the_same_file() -> None:
    """The screen and the offline verifier cannot disagree about one file."""
    cli = CliRunner().invoke(verify_cmd, ["receipt", str(_VALID), "--json"])
    assert cli.exit_code == 0, cli.output
    offline = json.loads(cli.output)

    served = json.loads(receipt_verdict_json(_VALID.read_bytes()))

    assert served["ok"] == offline["ok"]
    assert served["status"] == offline["status"]
    assert served["tier"] == offline["tier"]
    assert served["run_id"] == offline["run_id"]
    assert served["journal_events"] == offline["journal_events"]
    assert served["spine_entries"] == offline["spine_entries"]
    assert served["errors"] == offline["errors"]


def test_route_reports_a_tampered_receipt_with_200_and_a_failed_verdict(
    client: TestClient,
) -> None:
    """A receipt that does not verify is an answer about the evidence, not an HTTP error."""
    response = client.post("/governance/verify-receipt", content=_TAMPERED.read_bytes())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "tampered"
    assert body["tier"] is None


def test_route_reports_an_empty_drop_as_malformed(client: TestClient) -> None:
    """An empty upload gets a verdict, not a traceback."""
    response = client.post("/governance/verify-receipt", content=b"")

    assert response.status_code == 200
    assert response.json()["status"] == "malformed"
