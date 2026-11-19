"""Read-only recorded-decision API test vectors (#5070)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.routes import governance as routes
from bernstein.core.security import governance


def _client(workdir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(routes.router, prefix="/api/v1")
    app.state.workdir = workdir
    return TestClient(app)


def _record(workdir: Path, **fields: object) -> Path:
    directory = workdir / ".sdd" / "lineage" / "run-1" / "governance_decisions"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": "run-1",
        "subject": "alice",
        "action": "tasks:write",
        "verdict": "deny",
        "inputs_hash": "sha256:recorded-inputs",
        "timestamp": 1000,
        "context": {},
        "journal_entry_hash": "sha256:recorded-anchor",
    } | fields
    path = directory / "000000-record.json"
    path.write_text(json.dumps(record))
    return path


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def _budget_refusal(workdir: Path) -> tuple[SpendLedger, Path]:
    path = workdir / ".sdd" / "cost" / "ledger.jsonl"
    path.parent.mkdir(parents=True)
    ledger = SpendLedger(path=path)
    ledger.record(tags=CallTags(agent_id="alice"), model="test", cost_usd=9.0)
    with pytest.raises(governance.BudgetRefused):
        governance.check_budget_decision(
            run_id="run-1",
            lineage_root=workdir / ".sdd" / "lineage",
            hmac_key=b"synthetic-test-key-only",
            subject="alice",
            cap_usd=10.0,
            next_cost_usd=2.0,
            ledger_path=path,
            now=1000,
        )
    return ledger, path


def _list(client: TestClient) -> dict:
    response = client.get("/api/v1/governance/decisions", params={"run_id": "run-1"})
    assert response.status_code == 200, response.text
    return response.json()


def test_list_and_detail_preserve_recorded_fields_without_writes_or_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, context={"policy_version": "policy-17", "role": "viewer"})
    monkeypatch.setattr(routes, "_hmac_key", lambda: pytest.fail("read route must not access keys"))
    before = _snapshot(tmp_path)
    client = _client(tmp_path)
    (row,) = _list(client)["decisions"]
    assert row["subject"] == "alice"
    assert row["action"] == "tasks:write"
    assert row["verdict"] == "deny"
    assert row["inputs_hash"] == "sha256:recorded-inputs"
    assert row["journal_entry_hash"] == "sha256:recorded-anchor"
    assert row["policy_version"] == "policy-17"
    assert row["context"] == {"policy_version": "policy-17", "role": "viewer"}
    assert row["budget"] is None
    detail = client.get(f"/governance/decisions/{row['decision_id']}", params={"run_id": "run-1"})
    assert detail.status_code == 200
    assert detail.json() == row
    assert client.get("/governance/decisions/unknown", params={"run_id": "run-1"}).status_code == 404
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("context", [{}, {"policy_version": ""}, {"policy_version": None}])
def test_missing_policy_and_anchor_are_not_invented(tmp_path: Path, context: dict) -> None:
    _record(tmp_path, context=context, journal_entry_hash="")
    (row,) = _list(_client(tmp_path))["decisions"]
    assert row["policy_version"] == "this decision names no policy version"
    assert row["journal_entry_hash"] == ""


def test_empty_run_writes_nothing(tmp_path: Path) -> None:
    assert _list(_client(tmp_path)) == {"run_id": "run-1", "decisions": []}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("run_id", ["", "..", "../other", "a/b", "a\\b", "a\x00b"])
def test_invalid_run_ids_are_rejected(tmp_path: Path, run_id: str) -> None:
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": run_id}).status_code == 400


def test_outside_record_link_is_rejected(tmp_path: Path) -> None:
    record = _record(tmp_path)
    outside = tmp_path / "unrelated.json"
    outside.write_text(record.read_text())
    record.unlink()
    record.symlink_to(outside)
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"}).status_code == 400


def test_different_run_in_record_is_rejected(tmp_path: Path) -> None:
    _record(tmp_path, run_id="other-run")
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"}).status_code == 409


@pytest.mark.parametrize("state", ["verified", "unavailable", "mismatch"])
def test_budget_values_have_provenance_and_each_state(
    tmp_path: Path, state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, path = _budget_refusal(tmp_path)
    if state == "unavailable":
        path.unlink()
    elif state == "mismatch":
        ledger.record(tags=CallTags(agent_id="alice"), model="test", cost_usd=1.0)
    original = governance._verify_budget_row
    calls = []

    def verify(record: governance.GovernanceDecision, ledger_path: Path | None) -> str:
        calls.append(ledger_path)
        return original(record, ledger_path)

    monkeypatch.setattr(governance, "_verify_budget_row", verify)
    before = _snapshot(tmp_path)
    (row,) = _list(_client(tmp_path))["decisions"]
    budget = row["budget"]
    assert row["verdict"] == "refuse"
    assert budget["cap_usd"]["value"] == 10.0
    assert budget["prior_spend_usd"]["value"] == (9.0 if state == "verified" else None)
    for field in ("cap_usd", "prior_spend_usd"):
        assert budget[field]["state"] == state
        assert budget[field]["provenance"]
        assert budget[field]["reason"]
    assert "journal anchor and signer are not verified" in budget["verification_scope"]
    assert len(calls) == (0 if state == "unavailable" else 1)
    assert _snapshot(tmp_path) == before


def test_changing_ledger_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, _ = _budget_refusal(tmp_path)
    original = governance._verify_budget_row

    def verify(record: governance.GovernanceDecision, path: Path | None) -> str:
        result = original(record, path)
        ledger.record(tags=CallTags(agent_id="alice"), model="test", cost_usd=1.0)
        return result

    monkeypatch.setattr(governance, "_verify_budget_row", verify)
    (row,) = _list(_client(tmp_path))["decisions"]
    assert row["budget"]["prior_spend_usd"]["state"] == "unavailable"
    assert row["budget"]["prior_spend_usd"]["value"] is None
    assert "changed" in row["budget"]["prior_spend_usd"]["reason"]


def test_nonfinite_record_returns_controlled_error(tmp_path: Path) -> None:
    _record(tmp_path, context={"cap_usd": float("nan")})
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"}).status_code == 409


def test_nonobject_record_returns_controlled_error(tmp_path: Path) -> None:
    path = _record(tmp_path)
    path.write_text("[]")
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"}).status_code == 409


def test_public_schema_matches_response_models() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "api" / "recorded-decisions.schema.json"
    assert json.loads(path.read_text()) == routes.RecordedDecisions.model_json_schema()
