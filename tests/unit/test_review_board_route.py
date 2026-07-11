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
