"""Read-only recorded-decision API test vectors (#5070)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.routes import governance as routes
from bernstein.core.security import governance


@pytest.fixture(autouse=True)
def _platform_support(request: pytest.FixtureRequest) -> None:
    if not routes.ANCHORED_OPEN_SUPPORTED and request.node.name != "test_unsupported_platform_fails_closed":
        pytest.skip("descriptor-bound no-follow reads are unavailable on this platform")


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


def _budget_refusal(workdir: Path, *, prior_spend: float = 9.0) -> tuple[SpendLedger, Path]:
    path = workdir / ".sdd" / "cost" / "ledger.jsonl"
    path.parent.mkdir(parents=True)
    ledger = SpendLedger(path=path)
    ledger.record(tags=CallTags(agent_id="alice"), model="test", cost_usd=prior_spend)
    with pytest.raises(governance.BudgetRefused):
        governance.check_budget_decision(
            run_id="run-1",
            lineage_root=workdir / ".sdd" / "lineage",
            hmac_key=b"synthetic-test-key-only",
            subject="alice",
            cap_usd=prior_spend + 1.0,
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


@pytest.mark.parametrize("run_id", ["", "..", "../other", "a/b", "a\\b", "a\x00b", "a" * 201, "a b"])
def test_invalid_run_ids_are_rejected(tmp_path: Path, run_id: str) -> None:
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": run_id}).status_code == 400


def test_outside_record_link_is_rejected(tmp_path: Path) -> None:
    record = _record(tmp_path)
    outside = tmp_path / "unrelated.json"
    outside.write_text(record.read_text())
    record.unlink()
    record.symlink_to(outside)
    assert _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"}).status_code == 409


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


def test_overflowing_timestamp_returns_controlled_error_for_list_and_detail(tmp_path: Path) -> None:
    path = _record(tmp_path)
    client = _client(tmp_path)
    (healthy,) = _list(client)["decisions"]
    malformed = json.loads(path.read_text()) | {"timestamp": float("inf")}
    (path.parent / "000001-invalid.json").write_text(json.dumps(malformed))
    before = _snapshot(tmp_path)
    for route in ("/governance/decisions", f"/governance/decisions/{healthy['decision_id']}"):
        assert client.get(route, params={"run_id": "run-1"}).status_code == 409
    assert _snapshot(tmp_path) == before


def test_unsupported_platform_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(tmp_path)
    monkeypatch.setattr(routes, "ANCHORED_OPEN_SUPPORTED", False)
    response = _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"})
    assert response.status_code == 409
    assert "unavailable on this platform" in response.json()["detail"]


def test_record_open_refusal_after_enumeration_cannot_return_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path)
    original = os.open
    opened = []

    def open_record(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if str(path).endswith(".json"):
            opened.append(path)
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_NONBLOCK
            assert isinstance(kwargs.get("dir_fd"), int)
            # Deterministically model the OS refusing a replaced link at open;
            # no racing writer or outside-file read is needed for this test.
            raise OSError(errno.ELOOP, "no-follow open refused the record")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(routes.os, "open", open_record)
    monkeypatch.setattr(governance, "read_decisions", lambda *args: pytest.fail("unrestricted reader must not be used"))
    response = _client(tmp_path).get("/governance/decisions", params={"run_id": "run-1"})
    assert response.status_code == 409
    assert len(opened) == 1


def test_ledger_open_refusal_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _budget_refusal(tmp_path)
    original = routes.open_anchored

    def open_evidence(root: Path, *components: str, **kwargs: object) -> int:
        if components[-1] == "ledger.jsonl":
            assert kwargs["require_regular_file"]
            raise OSError(errno.ELOOP, "no-follow open refused the ledger")
        return original(root, *components, **kwargs)

    monkeypatch.setattr(routes, "open_anchored", open_evidence)
    (row,) = _list(_client(tmp_path))["decisions"]
    assert row["budget"]["prior_spend_usd"]["state"] == "unavailable"
    assert row["budget"]["prior_spend_usd"]["value"] is None


@pytest.mark.parametrize("alias_state", ["absent", "different-file"])
def test_unusable_descriptor_alias_never_verifies_zero_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias_state: str
) -> None:
    _budget_refusal(tmp_path, prior_spend=0.0)
    client = _client(tmp_path)
    (normal,) = _list(client)["decisions"]
    assert normal["budget"]["prior_spend_usd"]["state"] == "verified"
    alias = tmp_path / "descriptor-view"
    if alias_state == "different-file":
        alias.write_text("")

    class DescriptorView:
        def __truediv__(self, name: str) -> Path:
            return alias

    monkeypatch.setattr(routes, "Path", lambda name: DescriptorView())
    (row,) = _list(client)["decisions"]
    assert row["budget"]["prior_spend_usd"]["state"] == "unavailable"
    assert row["budget"]["prior_spend_usd"]["value"] is None


@pytest.mark.parametrize("stage", ["parsing", "serialization"])
def test_nesting_failure_returns_controlled_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    _record(tmp_path)
    original = json.dumps

    def fail_parsing(value: object, *args: object, **kwargs: object) -> object:
        raise RecursionError("simulated parser nesting limit")

    def fail_serialization(value: object, *args: object, **kwargs: object) -> str:
        if isinstance(value, dict) and "subject" in value:
            raise RecursionError("simulated serializer nesting limit")
        return original(value, *args, **kwargs)

    if stage == "parsing":
        monkeypatch.setattr(routes.json, "loads", fail_parsing)
    else:
        monkeypatch.setattr(routes.json, "dumps", fail_serialization)
    client = _client(tmp_path)
    for route in ("/governance/decisions", "/governance/decisions/unknown"):
        assert client.get(route, params={"run_id": "run-1"}).status_code == 409


def test_response_model_depth_failure_returns_controlled_error(tmp_path: Path) -> None:
    context = {}
    for _ in range(260):
        context = {"nested": context}
    path = _record(tmp_path, context=context)
    record = governance.GovernanceDecision.from_dict(json.loads(path.read_bytes()))
    decision_id = hashlib.sha256(json.dumps(record.to_dict(), sort_keys=True, allow_nan=False).encode()).hexdigest()
    before = _snapshot(tmp_path)
    client = _client(tmp_path)
    for route in ("/governance/decisions", f"/governance/decisions/{decision_id}"):
        assert client.get(route, params={"run_id": "run-1"}).status_code == 409
    assert path.read_bytes() == before[str(path.relative_to(tmp_path))]
    assert _snapshot(tmp_path) == before


def test_public_schema_matches_response_models() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "api" / "recorded-decisions.schema.json"
    assert json.loads(path.read_text()) == routes.RecordedDecisions.model_json_schema()
