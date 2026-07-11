"""Unit tests for the review-board API + page routes (#2365).

The endpoints are a read-only projection surface over the per-run event
journal: no board-side state, no writes. The page is served by both the
task server and ``bernstein gui serve`` (which mounts the same app).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.evidence.bundle import bundle_path
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.replay.review_board import (
    EVENT_TASK_MERGED,
    board_hash,
    canonical_board_bytes,
    project_board,
)
from bernstein.core.routes.review_board import router

if TYPE_CHECKING:
    from pathlib import Path


def _make_app(workdir: Path) -> FastAPI:
    """Minimal FastAPI shell wired to ``workdir`` for the board routes."""
    app = FastAPI()
    app.state.workdir = workdir
    app.state.sdd_dir = workdir / ".sdd"
    app.include_router(router)
    return app


def _write_run(workdir: Path, run_id: str) -> EventJournal:
    journal = EventJournal(run_id, workdir / ".sdd")
    journal.record("run_started", run_id=run_id, git_branch="main", git_sha="abc1234", config_hash="cfg")
    journal.record("task_claimed", task_id="t-1", agent_id="agent-a", model="model-x")
    journal.record("task_completed", task_id="t-1", agent_id="agent-a", cost_usd=0.2)
    journal.record(EVENT_TASK_MERGED, task_id="t-1", agent_id="agent-a")
    return journal


# ---------------------------------------------------------------------------
# Run listing
# ---------------------------------------------------------------------------


def test_list_runs_empty(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    response = client.get("/review-board/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_list_runs_returns_journal_backed_runs(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-a")
    _write_run(tmp_path, "run-b")
    client = TestClient(_make_app(tmp_path))
    response = client.get("/review-board/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": ["run-b", "run-a"]}


# ---------------------------------------------------------------------------
# Board projection endpoint (works against a detached run: journal on disk,
# no live orchestrator behind it)
# ---------------------------------------------------------------------------


def test_board_endpoint_projects_detached_run(tmp_path: Path) -> None:
    """The endpoint folds the on-disk journal; no orchestrator required."""
    journal = _write_run(tmp_path, "run-detached")
    client = TestClient(_make_app(tmp_path))

    response = client.get("/review-board/runs/run-detached")
    assert response.status_code == 200
    body = response.json()

    expected_board = project_board(load_events(journal.path))
    assert canonical_board_bytes(body["board"]) == canonical_board_bytes(expected_board)
    assert body["projection_hash"] == board_hash(expected_board)
    assert body["journal_head"] == journal.head()
    assert body["journal_verified"] is True
    assert body["event_count"] == 4
    assert body["run_id"] == "run-detached"


def test_board_endpoint_is_deterministic_across_requests(tmp_path: Path) -> None:
    """Two GETs of the same journal serve byte-identical board state."""
    _write_run(tmp_path, "run-two")
    client = TestClient(_make_app(tmp_path))

    first = client.get("/review-board/runs/run-two").json()
    time.sleep(0.01)
    second = client.get("/review-board/runs/run-two").json()

    assert canonical_board_bytes(first["board"]) == canonical_board_bytes(second["board"])
    assert first["projection_hash"] == second["projection_hash"]


def test_board_endpoint_unknown_run_404(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    response = client.get("/review-board/runs/run-missing")
    assert response.status_code == 404


@pytest.mark.parametrize("bad_run_id", ["..%2Fsecret", "a%2Fb", "run%00id"])
def test_board_endpoint_rejects_traversal_run_ids(tmp_path: Path, bad_run_id: str) -> None:
    """Path-shaped run ids are rejected before touching the filesystem."""
    client = TestClient(_make_app(tmp_path))
    response = client.get(f"/review-board/runs/{bad_run_id}")
    assert response.status_code in (400, 404)


def test_board_endpoint_flags_tampered_journal(tmp_path: Path) -> None:
    """A reviewer sees journal_verified=false when the chain does not verify."""
    journal = _write_run(tmp_path, "run-tampered")
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["task_id"] = "t-evil"
    lines[1] = json.dumps(row, default=str)
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    client = TestClient(_make_app(tmp_path))
    body = client.get("/review-board/runs/run-tampered").json()
    assert body["journal_verified"] is False


# ---------------------------------------------------------------------------
# Evidence bundle card endpoint (consumes #2362 sealed bundles)
# ---------------------------------------------------------------------------


def test_evidence_endpoint_404_when_absent(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-e")
    client = TestClient(_make_app(tmp_path))
    response = client.get("/review-board/runs/run-e/evidence/t-1")
    assert response.status_code == 404


def test_evidence_endpoint_returns_sealed_bundle(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-e2")
    path = bundle_path(tmp_path, "t-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "task_id": "t-1",
                "items": [
                    {
                        "name": "unit",
                        "kind": "test",
                        "required": True,
                        "status": "pass",
                        "exit_code": 0,
                        "content_hash": "sha256:" + "0" * 64,
                        "size": 10,
                        "truncated": False,
                    }
                ],
                "gate_passed": True,
                "timestamp": 1750000000,
                "signature": "sig",
                "journal_entry_hash": "jeh",
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(_make_app(tmp_path))
    response = client.get("/review-board/runs/run-e2/evidence/t-1")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "t-1"
    assert body["gate_passed"] is True
    assert body["items"][0]["name"] == "unit"
    assert body["bundle_hash"].startswith("sha256:")


@pytest.mark.parametrize("bad_task_id", ["..%2Fother", "t%2Fx"])
def test_evidence_endpoint_rejects_traversal_task_ids(tmp_path: Path, bad_task_id: str) -> None:
    _write_run(tmp_path, "run-e3")
    client = TestClient(_make_app(tmp_path))
    response = client.get(f"/review-board/runs/run-e3/evidence/{bad_task_id}")
    assert response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Board page (served by the task server and by `bernstein gui serve`)
# ---------------------------------------------------------------------------


def test_board_page_renders_columns_and_fetch_wiring(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    response = client.get("/dashboard/review-board")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    for column in ("queued", "running", "gated", "needs_review", "merged"):
        assert f'data-column="{column}"' in body
    assert "/review-board/runs" in body  # projection fetch is wired in
    assert "/events" in body  # SSE re-projection trigger is wired in


# ---------------------------------------------------------------------------
# Bearer auth coverage (existing task-server auth applies to the new surface)
# ---------------------------------------------------------------------------


@pytest.mark.auth_enabled
def test_board_routes_require_bearer_when_auth_configured(tmp_path: Path) -> None:
    from bernstein.core.auth_middleware import SSOAuthMiddleware

    _write_run(tmp_path, "run-auth")
    app = _make_app(tmp_path)
    app.add_middleware(SSOAuthMiddleware, legacy_token="sekret-token")
    client = TestClient(app)

    denied = client.get("/review-board/runs")
    assert denied.status_code == 401

    allowed = client.get("/review-board/runs", headers={"Authorization": "Bearer sekret-token"})
    assert allowed.status_code == 200
    assert allowed.json() == {"runs": ["run-auth"]}


# ---------------------------------------------------------------------------
# Diff viewer endpoint (content-addressed, journal-anchored review artifact)
# ---------------------------------------------------------------------------

from bernstein.core.replay.review_board import (
    record_task_diff_captured,
    store_task_diff,
)

_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n keep\n+added\n"


def _capture_diff(workdir: Path, run_id: str, task_id: str, journal: EventJournal, diff_text: str = _DIFF) -> dict:
    summary = store_task_diff(workdir / ".sdd", run_id, task_id, diff_text)
    assert summary is not None
    record_task_diff_captured(journal, task_id=task_id, summary=summary)
    return summary


def test_diff_endpoint_serves_stored_diff_verified(tmp_path: Path) -> None:
    journal = _write_run(tmp_path, "run-diff")
    summary = _capture_diff(tmp_path, "run-diff", "t-1", journal)
    client = TestClient(_make_app(tmp_path))

    response = client.get("/review-board/runs/run-diff/diff/t-1")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "t-1"
    assert body["diff_text"] == _DIFF
    assert body["sha256"] == summary["sha256"]
    assert body["added"] == 1
    assert body["files"] == ["x.py"]
    assert body["verified"] is True


def test_diff_endpoint_404_when_absent(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-nod")
    client = TestClient(_make_app(tmp_path))
    response = client.get("/review-board/runs/run-nod/diff/t-1")
    assert response.status_code == 404


def test_diff_endpoint_flags_unverified_on_tampered_bytes(tmp_path: Path) -> None:
    """Served diff bytes that no longer match the journal-recorded hash are flagged."""
    journal = _write_run(tmp_path, "run-tamp")
    _capture_diff(tmp_path, "run-tamp", "t-1", journal)
    diff_path = tmp_path / ".sdd" / "runs" / "run-tamp" / "review" / "diffs" / "t-1.diff"
    diff_path.write_text(_DIFF + "+sneaky\n", encoding="utf-8")

    client = TestClient(_make_app(tmp_path))
    body = client.get("/review-board/runs/run-tamp/diff/t-1").json()
    assert body["verified"] is False


@pytest.mark.parametrize("bad_task_id", ["..%2Fother", "t%2Fx"])
def test_diff_endpoint_rejects_traversal_task_ids(tmp_path: Path, bad_task_id: str) -> None:
    _write_run(tmp_path, "run-dt")
    client = TestClient(_make_app(tmp_path))
    response = client.get(f"/review-board/runs/run-dt/diff/{bad_task_id}")
    assert response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Attested action endpoint (approve / request_changes / merge)
# ---------------------------------------------------------------------------


def _make_dashboard_app(workdir: Path) -> tuple[FastAPI, dict[str, str]]:
    """Board app behind the real dashboard-auth middleware with scoped tokens."""
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.server.dashboard_auth import DashboardAuthMiddleware, DashboardAuthState
    from bernstein.core.server.dashboard_tokens import (
        SCOPE_OPERATOR,
        SCOPE_VIEWER,
        DashboardGovernance,
        DashboardTokenRegistry,
    )

    sdd = workdir / ".sdd"
    key = load_or_create_audit_key(sdd / "keys" / "audit.key")
    registry = DashboardTokenRegistry(sdd / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    viewer_raw, _ = registry.issue(principal="viewer-vera", scope=SCOPE_VIEWER, now=1000)
    operator_raw, _ = registry.issue(principal="operator-olga", scope=SCOPE_OPERATOR, now=1001)

    app = FastAPI()
    app.state.workdir = workdir
    app.state.sdd_dir = sdd
    app.include_router(router)
    state = DashboardAuthState(
        token_registry=registry,
        governance=DashboardGovernance(
            sdd / "lineage", hmac_key=key, audit_chain=AuditChainStore(sdd / "audit", key=key)
        ),
    )
    app.add_middleware(DashboardAuthMiddleware, state=state)
    return app, {"viewer": viewer_raw, "operator": operator_raw}


_ACTION = "/dashboard/review-board/runs/run-act/tasks/t-1/review"


def test_review_action_records_chained_and_signed_receipt(tmp_path: Path) -> None:
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.audit_chain import EVENT_REVIEW_BOARD_ACTION

    _write_run(tmp_path, "run-act")
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        _ACTION,
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert response.status_code == 200
    receipt = response.json()["receipt"]
    assert receipt["decision"] == "approve"
    assert receipt["principal"] == "operator-olga"
    assert receipt["task_id"] == "t-1"
    assert receipt["audit_event_hash"]

    # Chained receipt: the decision is a verifiable row in the run journal.
    journal_path = tmp_path / ".sdd" / "runs" / "run-act" / "journal.jsonl"
    events = load_events(journal_path)
    decisions = [e for e in events if e.get("event") == "task_review_decision"]
    assert len(decisions) == 1
    assert decisions[0]["principal"] == "operator-olga"
    assert verify_journal(journal_path).ok

    # Signed receipt: named principal on the HMAC-chained audit log.
    audit_events: list[dict] = []
    for log_file in sorted((tmp_path / ".sdd" / "audit").glob("*.jsonl")):
        audit_events.extend(load_events(log_file))
    board_actions = [e for e in audit_events if e.get("event_type") == EVENT_REVIEW_BOARD_ACTION]
    assert len(board_actions) == 1
    assert board_actions[0]["details"]["principal"] == "operator-olga"
    assert board_actions[0]["details"]["run_id"] == "run-act"


def test_review_action_merge_moves_card_to_merged_in_response(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-act")
    # Take t-1 back to needs_review first so merge is a real transition.
    journal = EventJournal.resume("run-act", tmp_path / ".sdd")
    journal.record("task_completed", task_id="t-2", agent_id="agent-b")
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/dashboard/review-board/runs/run-act/tasks/t-2/review",
        json={"decision": "merge"},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert response.status_code == 200
    board = response.json()["board"]["board"]
    merged_ids = [c["task_id"] for c in board["columns"]["merged"]]
    assert "t-2" in merged_ids


def test_review_action_requires_operator_scope(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-act")
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)

    denied = client.post(_ACTION, json={"decision": "approve"}, headers={"Authorization": f"Bearer {tokens['viewer']}"})
    assert denied.status_code == 403

    allowed = client.post(
        _ACTION, json={"decision": "approve"}, headers={"Authorization": f"Bearer {tokens['operator']}"}
    )
    assert allowed.status_code == 200


def test_review_action_rejects_unknown_decision(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-act")
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)
    response = client.post(
        _ACTION, json={"decision": "nuke"}, headers={"Authorization": f"Bearer {tokens['operator']}"}
    )
    assert response.status_code == 400


def test_review_action_unknown_run_404(tmp_path: Path) -> None:
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/dashboard/review-board/runs/run-missing/tasks/t-1/review",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["..%2Fx", "a%2Fb"])
def test_review_action_rejects_traversal_ids(tmp_path: Path, bad: str) -> None:
    _write_run(tmp_path, "run-act")
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)
    response = client.post(
        f"/dashboard/review-board/runs/{bad}/tasks/t-1/review",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert response.status_code in (400, 404)


def test_review_action_is_deterministic_projection_after_write(tmp_path: Path) -> None:
    """After an action, the board re-projects byte-identically across reads."""
    _write_run(tmp_path, "run-act")
    app, tokens = _make_dashboard_app(tmp_path)
    client = TestClient(app)
    client.post(_ACTION, json={"decision": "approve"}, headers={"Authorization": f"Bearer {tokens['operator']}"})

    first = client.get("/review-board/runs/run-act").json()
    second = client.get("/review-board/runs/run-act").json()
    assert canonical_board_bytes(first["board"]) == canonical_board_bytes(second["board"])
    assert first["projection_hash"] == second["projection_hash"]
    assert first["journal_verified"] is True


def test_board_page_wires_diff_viewer_and_actions(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    body = client.get("/dashboard/review-board").text
    assert "/diff/" in body
    assert "/review" in body  # action POST path fragment
    for label in ("Approve", "Request changes", "Merge"):
        assert label in body
