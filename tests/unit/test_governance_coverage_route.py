"""``GET /governance/coverage`` returns the projection, not a second opinion (#5067).

The route owns no arithmetic: it parses the query string, calls
:func:`bernstein.core.security.governance_coverage.governance_coverage_json`
and returns those bytes verbatim, so the dashboard and an offline recomputation
from ``.sdd`` cannot disagree about a coverage number.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.routes.governance import router as governance_router
from bernstein.core.security.governance_coverage import governance_coverage_json

_KEY = b"0" * 32


@pytest.fixture(autouse=True)
def _pin_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bernstein.core.routes.governance._hmac_key", lambda: _KEY)


def _client(workdir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(governance_router)
    app.state.workdir = workdir
    return TestClient(app)


def _act(workdir: Path, *, actor: str, path: str) -> None:
    LineageSpine(workdir / ".sdd" / "lineage", run_id="run-1", hmac_key=_KEY).record(
        artifact_path=path,
        content=path.encode(),
        actor=actor,
        step_id="write",
        model="none",
        timestamp=1000,
    )


def test_route_returns_the_projection_bytes_verbatim(tmp_path: Path) -> None:
    _act(tmp_path, actor="agent-writer", path="src/a.py")

    response = _client(tmp_path).get("/governance/coverage", params={"run_id": "run-1"})

    assert response.status_code == 200
    assert response.text == governance_coverage_json(tmp_path, "run-1", hmac_key=_KEY)


def test_route_rejects_a_missing_run_id(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/governance/coverage")

    assert response.status_code == 400


def test_route_rejects_a_traversing_run_id(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/governance/coverage", params={"run_id": "../escape"})

    assert response.status_code == 400
