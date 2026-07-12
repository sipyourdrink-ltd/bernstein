"""End-to-end review cycle against a live-run journal (#2365, plan item 5).

The board's action tests elsewhere drive hand-written journal fixtures. This
module proves the full review cycle - inspect diff, read evidence, approve,
merge - against a journal produced by the *real* task-lifecycle reap seam
(``_reap_and_cleanup_session``), driven through the *real* dashboard-auth
middleware with a scoped operator principal.

The only mocked surfaces are the external process/git boundaries the seam
already stubs in its own unit tests (agent reap, worktree cleanup, worktree
git diff). The production code paths under test - diff capture, journal
chaining, projection, evidence read, and the attested action endpoint - run
for real, so the four acceptance criteria are exercised against a live run,
not a fixture.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.evidence.bundle import EvidenceBundle, EvidenceItem, bundle_path
from bernstein.core.replay.journal import EventJournal, load_events, verify_journal
from bernstein.core.replay.review_board import canonical_board_bytes, project_run
from bernstein.core.routes.review_board import router
from bernstein.core.security.audit_chain import EVENT_REVIEW_BOARD_ACTION

if TYPE_CHECKING:
    from pathlib import Path

_LIVE_DIFF = """diff --git a/src/pkg/mod.py b/src/pkg/mod.py
index aaa1111..bbb2222 100644
--- a/src/pkg/mod.py
+++ b/src/pkg/mod.py
@@ -1,3 +1,4 @@
 header
-old line
+new line
+added line
"""


def _drive_live_run_to_needs_review(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
    run_id: str,
    task_id: str,
) -> EventJournal:
    """Produce a run journal via the real reap seam.

    A completed but unmerged task (approval-gated ``skip_merge``) lands in
    ``needs_review`` with a captured review diff, awaiting an operator decision.
    """
    from bernstein.core.tasks import task_lifecycle

    # External boundaries the seam already stubs in its own unit tests.
    monkeypatch.setattr(task_lifecycle, "_close_completed_task", lambda *_a, **_k: None)
    monkeypatch.setattr(task_lifecycle, "seal_evidence_on_completion", lambda *_a, **_k: None)
    monkeypatch.setattr(task_lifecycle, "_get_git_diff_text_in_worktree", lambda _wt: _LIVE_DIFF)

    recorder = EventJournal(run_id, tmp_path / ".sdd")
    recorder.record("run_started", run_id=run_id, git_branch="main", git_sha="deadbee", config_hash="cfg")
    recorder.record("task_claimed", task_id=task_id, agent_id="sess-live", model="model-x")
    recorder.record("task_completed", task_id=task_id, agent_id="sess-live", cost_usd=0.4)

    spawner = SimpleNamespace(
        reap_completed_agent=lambda *_a, **_k: None,
        cleanup_worktree=lambda _sid: None,
        get_worktree_path=lambda _sid: tmp_path / "wt",
    )
    orch = SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _config=SimpleNamespace(ab_test=False),
        _recorder=recorder,
    )
    session = SimpleNamespace(id="sess-live", status="dead", exit_code=0, task_ids=[task_id])
    task = SimpleNamespace(id=task_id, metadata={})

    task_lifecycle._reap_and_cleanup_session(
        orch,
        task,
        session,
        None,
        janitor_passed=True,
        skip_merge=True,
        _completion_data=None,
        cache_diff_lines=0,
    )
    return recorder


def _seal_evidence_bundle(tmp_path: Path, task_id: str) -> None:
    """Write a real sealed evidence bundle the evidence endpoint can serve."""
    bundle = EvidenceBundle(
        task_id=task_id,
        items=(
            EvidenceItem(
                name="unit",
                kind="test",
                required=True,
                status="pass",
                exit_code=0,
                content_hash="sha256:" + "0" * 64,
                size=42,
                truncated=False,
            ),
        ),
        gate_passed=True,
        timestamp=1750000000,
        signature="sig",
        journal_entry_hash="jeh",
    )
    path = bundle_path(tmp_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle.to_dict()), encoding="utf-8")


def _dashboard_app(workdir: Path) -> tuple[FastAPI, dict[str, str]]:
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


def _post_review(client: TestClient, run_id: str, task_id: str, decision: str, token: str):  # type: ignore[no-untyped-def]
    return client.post(
        f"/dashboard/review-board/runs/{run_id}/tasks/{task_id}/review",
        json={"decision": decision},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_full_review_cycle_against_live_run(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Inspect diff, read evidence, approve, then merge - all on the board."""
    run_id, task_id = "run-live", "t-live"
    _drive_live_run_to_needs_review(tmp_path, monkeypatch, run_id, task_id)
    _seal_evidence_bundle(tmp_path, task_id)
    app, tokens = _dashboard_app(tmp_path)
    client = TestClient(app)

    # 1. Projection: the live run's card is in needs_review, journal verified.
    projection = client.get(f"/review-board/runs/{run_id}")
    assert projection.status_code == 200
    body = projection.json()
    assert body["journal_verified"] is True
    needs_review = [c["task_id"] for c in body["board"]["columns"]["needs_review"]]
    assert task_id in needs_review

    # 2. Inspect diff: served bytes verify against the journal-chained hash.
    diff = client.get(f"/review-board/runs/{run_id}/diff/{task_id}").json()
    assert diff["verified"] is True
    assert diff["diff_text"] == _LIVE_DIFF
    assert diff["files"] == ["src/pkg/mod.py"]

    # 3. View evidence: the sealed bundle is served with a recomputed hash.
    evidence = client.get(f"/review-board/runs/{run_id}/evidence/{task_id}").json()
    assert evidence["gate_passed"] is True
    assert evidence["items"][0]["name"] == "unit"
    assert evidence["bundle_hash"].startswith("sha256:")

    # 4. Approve: chained + signed receipt naming the operator principal.
    approve = _post_review(client, run_id, task_id, "approve", tokens["operator"])
    assert approve.status_code == 200
    assert approve.json()["receipt"]["principal"] == "operator-olga"

    # 5. Merge: the card moves to merged in the returned projection, no reload.
    merge = _post_review(client, run_id, task_id, "merge", tokens["operator"])
    assert merge.status_code == 200
    merged = [c["task_id"] for c in merge.json()["board"]["board"]["columns"]["merged"]]
    assert task_id in merged

    # The whole cycle stayed chained: the journal re-verifies end to end and
    # every decision row names the acting principal.
    journal_path = tmp_path / ".sdd" / "runs" / run_id / "journal.jsonl"
    assert verify_journal(journal_path).ok
    events = load_events(journal_path)
    decisions = [e for e in events if e.get("event") == "task_review_decision"]
    assert [d["decision"] for d in decisions] == ["approve", "merge"]
    assert all(d["principal"] == "operator-olga" for d in decisions)

    # Both actions are mirrored as signed audit receipts naming the principal.
    audit_events: list[dict] = []
    for log_file in sorted((tmp_path / ".sdd" / "audit").glob("*.jsonl")):
        audit_events.extend(load_events(log_file))
    board_actions = [e for e in audit_events if e.get("event_type") == EVENT_REVIEW_BOARD_ACTION]
    assert len(board_actions) == 2
    assert all(e["details"]["principal"] == "operator-olga" for e in board_actions)


def test_viewer_scope_cannot_drive_the_live_review_cycle(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A read-only principal is refused before any decision is chained."""
    run_id, task_id = "run-live-ro", "t-live"
    _drive_live_run_to_needs_review(tmp_path, monkeypatch, run_id, task_id)
    app, tokens = _dashboard_app(tmp_path)
    client = TestClient(app)

    denied = _post_review(client, run_id, task_id, "merge", tokens["viewer"])
    assert denied.status_code == 403

    # No decision row was appended: the card is still awaiting review.
    events = load_events(tmp_path / ".sdd" / "runs" / run_id / "journal.jsonl")
    assert [e for e in events if e.get("event") == "task_review_decision"] == []


def test_live_run_projects_byte_identical_when_detached(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The live-run journal, copied to a detached location, projects identically.

    Covers AC3 (byte-identical replay) and AC4 (detached run) against a journal
    the real reap seam produced, not a hand-written fixture.
    """
    import shutil

    run_id, task_id = "run-live-det", "t-live"
    _drive_live_run_to_needs_review(tmp_path, monkeypatch, run_id, task_id)

    live = project_run(tmp_path / ".sdd", run_id)
    assert live is not None

    detached_root = tmp_path / "detached"
    shutil.copytree(tmp_path / ".sdd", detached_root / ".sdd")
    detached = project_run(detached_root / ".sdd", run_id)
    assert detached is not None

    assert canonical_board_bytes(live.board) == canonical_board_bytes(detached.board)
    assert live.projection_hash == detached.projection_hash
    assert live.journal_head == detached.journal_head
    assert detached.journal_verified is True
