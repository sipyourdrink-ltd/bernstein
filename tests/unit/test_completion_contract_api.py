"""API-boundary tests for schema-enforced completion payloads (#2244).

Covers the acceptance criteria at the ``POST /tasks/{id}/complete``
endpoint:

    1. Malformed completion JSON produces a ``contract_violation``
       failure carrying the schema error path, never an accepted task.
    2. Refusal kinds are a closed enum; an unknown kind is a contract
       violation.
    3. A ``scope_exceeded`` refusal deterministically produces the same
       follow-up task set from the same payload (idempotent on redelivery).
    4. Contract version and validation outcome land in the task record
       and the HMAC-chained audit log, and verify against the chain.
    5. Refused tasks are counted separately from failed tasks.

Legacy prose summaries remain accepted unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.auth_rate_limiter import RequestRateLimitMiddleware
from starlette.testclient import TestClient

from bernstein.core.security.audit import AuditLog
from bernstein.core.server import create_app
from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION
from bernstein.core.tasks.lifecycle import set_audit_log

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# One app per module: repeated create_app + TestClient startup cycles in a
# single process exhaust the recursion limit, and per-test isolation is
# already guaranteed by unique task ids. The module-scoped fixture is built
# before the function-scoped autouse auth fixture, so disable auth here.
@pytest.fixture(scope="module")
def app_env(tmp_path_factory: pytest.TempPathFactory) -> Any:
    previous_auth = os.environ.get("BERNSTEIN_AUTH_DISABLED")
    os.environ["BERNSTEIN_AUTH_DISABLED"] = "1"
    try:
        workdir = tmp_path_factory.mktemp("contract-api")
        app = create_app(jsonl_path=workdir / ".sdd" / "runtime" / "tasks.jsonl")
        with TestClient(app) as test_client:
            yield test_client, workdir
    finally:
        if previous_auth is None:
            os.environ.pop("BERNSTEIN_AUTH_DISABLED", None)
        else:
            os.environ["BERNSTEIN_AUTH_DISABLED"] = previous_auth


@pytest.fixture
def client(app_env: tuple[TestClient, Path]) -> TestClient:
    test_client = app_env[0]
    # Clear rate limiter hit counters between tests on the shared app.
    mw = test_client.app.middleware_stack  # type: ignore[union-attr]
    while mw is not None:
        if isinstance(mw, RequestRateLimitMiddleware):
            mw._limiter._hits.clear()  # type: ignore[attr-defined]
            break
        mw = getattr(mw, "app", None)
    return test_client


@pytest.fixture
def server_workdir(app_env: tuple[TestClient, Path]) -> Path:
    return app_env[1]


def _create_and_claim(client: TestClient, title: str = "Contract test task") -> str:
    create_resp = client.post(
        "/tasks",
        json={
            "title": title,
            "description": "Exercise the completion contract.",
            "role": "backend",
            "priority": 2,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 5,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    task_id: str = create_resp.json()["id"]
    assert client.post(f"/tasks/{task_id}/claim").status_code == 200
    return task_id


def _completion_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract": WORKER_CONTRACT_VERSION,
        "summary": "Implemented and verified.",
        "files_changed": ["src/foo.py"],
        "verification": {"command": "pytest -q", "exit_code": 0},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Legacy path stays intact
# ---------------------------------------------------------------------------


def test_legacy_prose_summary_still_accepted(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(f"/tasks/{task_id}/complete", json={"result_summary": "Did the thing."})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "done"


# ---------------------------------------------------------------------------
# AC1: malformed payloads are typed contract violations
# ---------------------------------------------------------------------------


def test_structured_completion_accepted(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(f"/tasks/{task_id}/complete", json={"payload": _completion_payload()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["result_summary"] == "Implemented and verified."
    assert body["metadata"]["contract_version"] == WORKER_CONTRACT_VERSION
    assert body["metadata"]["contract_validation"] == "valid"
    assert body["metadata"]["worker_completion"]["files_changed"] == ["src/foo.py"]


def test_invalid_payload_fails_task_with_schema_path(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    payload = _completion_payload()
    del payload["summary"]
    resp = client.post(f"/tasks/{task_id}/complete", json={"payload": payload})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "contract_violation"
    assert detail["schema_error_path"] == "$.summary"
    assert detail["contract_version"] == WORKER_CONTRACT_VERSION
    # The task is a typed failure, never an accepted completion.
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["terminal_reason"] == "contract_violation"


def test_malformed_inline_json_summary_is_contract_violation(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": '{"summary": "unterminated'},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "contract_violation"
    assert detail["schema_error_path"] == "$"
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"


def test_valid_inline_json_summary_parses_as_contract(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": json.dumps(_completion_payload(summary="Inline JSON payload."))},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["result_summary"] == "Inline JSON payload."
    assert body["metadata"]["contract_validation"] == "valid"


# ---------------------------------------------------------------------------
# AC2: closed refusal enum
# ---------------------------------------------------------------------------


def test_refusal_lands_task_in_refused_state(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(
        f"/tasks/{task_id}/complete",
        json={
            "payload": {
                "kind": "blocked_on_dependency",
                "detail": "Upstream schema is not merged yet.",
                "blocking_dep": "T-99",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refused"
    assert body["terminal_reason"] == "refused:blocked_on_dependency"
    assert body["metadata"]["refusal"]["blocking_dep"] == "T-99"


def test_unknown_refusal_kind_is_contract_violation(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(
        f"/tasks/{task_id}/complete",
        json={"payload": {"kind": "gave_up", "detail": "nope"}},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "contract_violation"
    assert detail["schema_error_path"] == "$.kind"
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["terminal_reason"] == "contract_violation"


# ---------------------------------------------------------------------------
# AC3: deterministic scope_exceeded routing
# ---------------------------------------------------------------------------


def _scope_exceeded_payload() -> dict[str, Any]:
    return {
        "kind": "scope_exceeded",
        "detail": "This spans two subsystems.",
        "proposed_split": ["extract the parser", "wire the endpoint"],
    }


def _open_task_ids(client: TestClient) -> set[str]:
    tasks = client.get("/tasks").json()
    return {t["id"] for t in tasks if t["status"] == "open"}


def test_scope_exceeded_creates_follow_up_tasks(client: TestClient) -> None:
    task_id = _create_and_claim(client)
    before = _open_task_ids(client)
    resp = client.post(f"/tasks/{task_id}/complete", json={"payload": _scope_exceeded_payload()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "refused"
    created = _open_task_ids(client) - before
    assert len(created) == 2
    tasks_by_id = {t["id"]: t for t in client.get("/tasks").json()}
    for new_id in created:
        follow_up = tasks_by_id[new_id]
        assert follow_up["parent_task_id"] == task_id
        assert follow_up["role"] == "backend"


def test_scope_exceeded_follow_ups_are_deterministic(client: TestClient) -> None:
    # Two identical refusals on two servers rooted at different stores must
    # derive the same follow-up ids for the same parent task id; and a
    # redelivered refusal must not duplicate follow-ups on one server.
    task_id = _create_and_claim(client)
    before = _open_task_ids(client)
    assert client.post(f"/tasks/{task_id}/complete", json={"payload": _scope_exceeded_payload()}).status_code == 200
    first = _open_task_ids(client) - before

    # Redelivery: the task is already terminal, so /complete conflicts, and
    # no additional follow-up tasks appear.
    resp = client.post(f"/tasks/{task_id}/complete", json={"payload": _scope_exceeded_payload()})
    assert resp.status_code in (409, 422)
    assert _open_task_ids(client) - before == first

    from bernstein.core.tasks.contracts import derive_follow_up_specs, parse_terminal_payload

    parsed = parse_terminal_payload(_scope_exceeded_payload())
    expected = {spec.task_id for spec in derive_follow_up_specs(task_id, parsed)}  # type: ignore[arg-type]
    assert first == expected


# ---------------------------------------------------------------------------
# awaiting_operator surfaces as an operator approval item
# ---------------------------------------------------------------------------


def test_awaiting_operator_writes_pending_approval_item(client: TestClient, server_workdir: Path) -> None:
    task_id = _create_and_claim(client)
    resp = client.post(
        f"/tasks/{task_id}/complete",
        json={
            "payload": {
                "kind": "awaiting_operator",
                "detail": "Deleting the legacy config needs sign-off.",
                "question": "May I delete config/legacy.yaml?",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "refused"
    pending = server_workdir / ".sdd" / "runtime" / "pending_approvals" / f"{task_id}.json"
    assert pending.is_file()
    item = json.loads(pending.read_text(encoding="utf-8"))
    assert item["task_id"] == task_id
    assert "May I delete config/legacy.yaml?" in item["test_summary"]


# ---------------------------------------------------------------------------
# AC4: contract version + outcome in the audit chain
# ---------------------------------------------------------------------------


def test_contract_outcome_verifies_against_audit_chain(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit", key=b"test-key-0123456789")
    set_audit_log(audit)
    try:
        app = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
        with TestClient(app) as client:
            task_id = _create_and_claim(client)
            resp = client.post(f"/tasks/{task_id}/complete", json={"payload": _completion_payload()})
            assert resp.status_code == 200, resp.text
    finally:
        set_audit_log(None)  # type: ignore[arg-type]
    valid, errors = audit.verify()
    assert valid, errors
    events = [
        json.loads(line)
        for f in sorted((tmp_path / "audit").glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    contract_events = [e for e in events if e["event_type"] == "task.contract_validation"]
    assert len(contract_events) == 1
    assert contract_events[0]["resource_id"] == task_id
    assert contract_events[0]["details"]["contract_version"] == WORKER_CONTRACT_VERSION
    assert contract_events[0]["details"]["outcome"] == "valid"


# ---------------------------------------------------------------------------
# AC5: refused vs failed counts
# ---------------------------------------------------------------------------


def test_counts_distinguish_refused_from_failed(client: TestClient) -> None:
    before = client.get("/tasks/counts").json()
    refused_id = _create_and_claim(client, title="Refused task")
    failed_id = _create_and_claim(client, title="Failed task")
    assert (
        client.post(
            f"/tasks/{refused_id}/complete",
            json={"payload": {"kind": "underspecified", "detail": "No backend named.", "question": "Which one?"}},
        ).status_code
        == 200
    )
    assert client.post(f"/tasks/{failed_id}/fail", json={"reason": "boom"}).status_code == 200
    counts = client.get("/tasks/counts").json()
    assert counts["refused"] == before["refused"] + 1
    assert counts["failed"] == before["failed"] + 1
