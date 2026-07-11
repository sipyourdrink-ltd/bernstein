"""Review-board routes: a web merge queue projected from the run journal (#2365).

Reviewing many parallel tasks currently spans TUI panes and terminal logs.
These endpoints put the queue, the evidence, and the gate receipts on one
web surface while keeping the board a pure projection - the server folds
the per-run event journal on every request and the client renders whatever
the fold returns. There is no board-side state anywhere.

* ``GET /review-board/runs`` - run ids with a journal, newest first.
* ``GET /review-board/runs/{run_id}`` - the board projection receipt:
  canonical board state plus ``projection_hash``, ``journal_head``, and
  ``journal_verified`` so a reviewer can prove the rendered board equals
  the executed journal. Works against a detached run (journal on disk, no
  live orchestrator) exactly like a live one.
* ``GET /review-board/runs/{run_id}/evidence/{task_id}`` - the task's
  sealed evidence bundle (#2362) with its ``bundle_hash``, for the card
  drawer.
* ``GET /review-board/runs/{run_id}/diff/{task_id}`` - the task's captured
  diff (content-addressed beside the run journal, so it serves against a
  detached run) plus a ``verified`` flag cross-checking the served bytes
  against the journal-chained capture hash. The card drawer folds it.
* ``POST /dashboard/review-board/runs/{run_id}/tasks/{task_id}/review`` -
  the attested action endpoint (approve / request-changes / merge). The
  decision is appended to the run journal (a chained receipt the board's
  merged column and review annotations project from) and mirrored as a
  signed ``review_board.action`` audit entry naming the acting principal.
  It sits under ``/dashboard`` so the dashboard-auth middleware attributes
  a real principal and blocks any non-operator scope.
* ``GET /dashboard/review-board`` - dependency-free HTML board page. Also
  served by ``bernstein gui serve`` (which mounts this same app), and
  refreshed from the existing ``/events`` SSE stream.

Bearer auth: the task server's auth middleware covers every read route here
(none is registered as a public path); the action endpoint is additionally
scope-gated by the dashboard-auth middleware.

Deliberate non-goals (board scope): no editing, no chat, no board-side
state. A board action is a governance receipt, never board-side state - the
run journal is the only place a decision lives, and the board is a pure
projection of it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from bernstein.core.evidence.bundle import read_evidence_bundle
from bernstein.core.replay.review_board import (
    BOARD_COLUMNS,
    REVIEW_DECISIONS,
    BoardProjection,
    list_board_runs,
    project_run,
    read_task_diff,
    record_review_decision,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Principal / scope attributed to a board action when no dashboard credential
#: is attached (an unconfigured loopback bind or an embedded test app). Real
#: deployments arrive with ``request.state.dashboard_principal`` stamped by the
#: dashboard-auth middleware, whose scope gate already blocks non-operators.
_LOOPBACK_ACTION_PRINCIPAL: Final[str] = "dashboard-operator"
_LOOPBACK_ACTION_SCOPE: Final[str] = "operator"


class ReviewActionRequest(BaseModel):
    """Body of a board review action (approve / request-changes / merge)."""

    decision: str
    note: str = ""


#: Run ids are directory names under ``.sdd/runs/``; reject anything that
#: could carry a path separator, a NUL, or HTML-active characters before
#: the value touches the filesystem or the page.
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

#: Task ids are slug-shaped (``t-xxx``). The evidence store content-hashes
#: ids before building paths, so this check is defense in depth plus a
#: cheap 400 for garbage input.
_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _resolve_workdir(request: Request) -> Path:
    """Locate the project root from app state (mirrors the trace routes)."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path) and sdd_dir.name == ".sdd":
        return sdd_dir.parent
    return Path.cwd()


def _validate_run_id(run_id: str) -> str:
    """Reject run ids that are not plain directory-name slugs."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")
    return run_id


def _validate_task_id(task_id: str) -> str:
    """Reject task ids that are not plain slugs."""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=400, detail="invalid task id")
    return task_id


def _contained_run_dir(sdd_dir: Path, run_id: str) -> Path:
    """Resolve ``runs/<run_id>`` and verify it stays inside the runs root."""
    runs_root = (sdd_dir / "runs").resolve()
    resolved = (runs_root / run_id).resolve()
    if resolved != runs_root and not resolved.is_relative_to(runs_root):
        raise HTTPException(status_code=400, detail="invalid run id")
    return resolved


# ---------------------------------------------------------------------------
# Projection API
# ---------------------------------------------------------------------------


@router.get("/review-board/runs")
def review_board_runs(request: Request) -> JSONResponse:
    """List run ids that have a journal to project, newest first."""
    sdd_dir = _resolve_workdir(request) / ".sdd"
    return JSONResponse({"runs": list_board_runs(sdd_dir)})


@router.get("/review-board/runs/{run_id}")
def review_board_projection(run_id: str, request: Request) -> JSONResponse:
    """Serve the board projection receipt for ``run_id``.

    The response is a deterministic function of the run's journal file:
    the same journal bytes serve the same ``board`` and
    ``projection_hash`` from any server, so a reviewer can cross-check two
    operators (or the API against a local ``project_run`` fold) byte for
    byte. ``journal_verified=false`` marks a chain that no longer
    recomputes - the board is still rendered but must not be trusted.
    """
    run_id = _validate_run_id(run_id)
    sdd_dir = _resolve_workdir(request) / ".sdd"
    _contained_run_dir(sdd_dir, run_id)
    projection = project_run(sdd_dir, run_id)
    if projection is None:
        raise HTTPException(status_code=404, detail=f"no journal for run: {run_id}")
    return JSONResponse(projection.to_dict())


@router.get("/review-board/runs/{run_id}/evidence/{task_id}")
def review_board_evidence(run_id: str, task_id: str, request: Request) -> JSONResponse:
    """Serve the sealed evidence bundle for a board card.

    The bundle is the #2362 proof-of-done artifact: content-addressed
    items, the gate verdict, the producing signature, and the audit-chain
    entry hash. ``bundle_hash`` is recomputed from the canonical binding
    bytes on every read so the drawer always shows the bundle's current
    identity.
    """
    _validate_run_id(run_id)
    task_id = _validate_task_id(task_id)
    workdir = _resolve_workdir(request)
    bundle = read_evidence_bundle(workdir, task_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"no evidence bundle for task: {task_id}")
    payload = bundle.to_dict()
    payload["bundle_hash"] = bundle.bundle_hash()
    return JSONResponse(payload)


def _card_diff_sha(board: dict[str, Any], task_id: str) -> str:
    """Return the journal-recorded diff hash for a card, or ``""``."""
    for cards in board["columns"].values():
        for card in cards:
            if card.get("task_id") == task_id and card.get("diff"):
                return str(card["diff"].get("sha256", ""))
    return ""


@router.get("/review-board/runs/{run_id}/diff/{task_id}")
def review_board_diff(run_id: str, task_id: str, request: Request) -> JSONResponse:
    """Serve the captured task diff for the card drawer's diff viewer.

    The diff bytes were captured beside the run journal at completion time
    (``task_diff_captured``), so they are exactly what executed and are
    available against a detached run - no live ``git`` at review time. The
    served bytes are re-hashed and cross-checked against the journal-chained
    capture hash: ``verified`` is ``true`` only when the diff a reviewer folds
    open equals the diff that was captured and the chain still recomputes.
    """
    run_id = _validate_run_id(run_id)
    task_id = _validate_task_id(task_id)
    sdd_dir = _resolve_workdir(request) / ".sdd"
    _contained_run_dir(sdd_dir, run_id)
    result = read_task_diff(sdd_dir, run_id, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no captured diff for task: {task_id}")
    diff_text, summary = result
    projection = project_run(sdd_dir, run_id)
    recorded = _card_diff_sha(projection.board, task_id) if projection is not None else ""
    verified = (
        bool(recorded) and projection is not None and projection.journal_verified and recorded == summary["sha256"]
    )
    return JSONResponse(
        {
            "run_id": run_id,
            "task_id": task_id,
            "sha256": summary["sha256"],
            "recorded_sha256": recorded,
            "verified": verified,
            "added": summary["added"],
            "removed": summary["removed"],
            "files": summary["files"],
            "files_changed": summary["files_changed"],
            "diff_text": diff_text,
        }
    )


# ---------------------------------------------------------------------------
# Attested action endpoint (approve / request-changes / merge)
#
# A board action is a governance receipt, not board-side state: the operator's
# decision is appended to the run journal (the same Merkle chain the execution
# lives in, so it chains onto the exact head it was made against) and mirrored
# as a signed, principal-named ``review_board.action`` entry on the audit
# chain. The board's merged column and review annotations then project from
# that journal row - strip the chain and the action loses its meaning, not
# just its log line. The route sits under ``/dashboard`` so the dashboard-auth
# middleware attributes a real principal and blocks any non-operator scope.
# ---------------------------------------------------------------------------


@router.post("/dashboard/review-board/runs/{run_id}/tasks/{task_id}/review")
def review_board_action(
    run_id: str,
    task_id: str,
    payload: ReviewActionRequest,
    request: Request,
) -> JSONResponse:
    """Record an operator board decision as a chained, signed receipt.

    The scope gate is enforced upstream by the dashboard-auth middleware
    (operator scope required for this write); the acting principal arrives on
    ``request.state.dashboard_principal``. The decision row is appended via
    ``EventJournal.resume`` so it chains onto the verified journal tail and
    fails closed on a poisoned chain (``409``).
    """
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore, record_review_board_action
    from bernstein.core.server.dashboard_tokens import resolve_dashboard_hmac_key

    run_id = _validate_run_id(run_id)
    task_id = _validate_task_id(task_id)
    decision = payload.decision
    if decision not in REVIEW_DECISIONS:
        raise HTTPException(status_code=400, detail=f"invalid decision: {decision!r}")

    sdd_dir = _resolve_workdir(request) / ".sdd"
    _contained_run_dir(sdd_dir, run_id)

    before: BoardProjection | None = project_run(sdd_dir, run_id)
    if before is None:
        raise HTTPException(status_code=404, detail=f"no journal for run: {run_id}")

    principal = getattr(request.state, "dashboard_principal", "") or _LOOPBACK_ACTION_PRINCIPAL
    scope = getattr(request.state, "dashboard_scope", "") or _LOOPBACK_ACTION_SCOPE
    diff_hash = _card_diff_sha(before.board, task_id)

    try:
        journal = EventJournal.resume(run_id, sdd_dir)
    except ValueError as exc:
        # A run journal whose chain no longer verifies must not be extended
        # from a poisoned anchor. Log the exception type only (chain-adjacent).
        raise HTTPException(
            status_code=409,
            detail=f"run journal chain does not verify ({type(exc).__name__}); refusing to append",
        ) from exc

    record_review_decision(
        journal,
        task_id=task_id,
        decision=decision,
        principal=principal,
        scope=scope,
        note=payload.note,
    )
    journal_entry_hash = journal.head()

    audit_event_hash = ""
    try:
        chain = AuditChainStore(sdd_dir / "audit", key=resolve_dashboard_hmac_key(sdd_dir))
        event = record_review_board_action(
            chain=chain,
            run_id=run_id,
            task_id=task_id,
            decision=decision,
            principal=principal,
            scope=scope,
            projection_hash=before.projection_hash,
            journal_head=before.journal_head,
            diff_hash=diff_hash,
            journal_entry_hash=journal_entry_hash,
        )
        audit_event_hash = event.hmac
    except Exception as exc:  # intentional-broad-except
        # Audit mirror is best-effort: the chained journal receipt already
        # stands and is the board's source of truth; the audit entry is a
        # convenience projection. Log the type only (audit-key-adjacent path).
        logger.warning("review_board.action audit mirror failed (%s)", type(exc).__name__)

    after = project_run(sdd_dir, run_id)
    return JSONResponse(
        {
            "receipt": {
                "run_id": run_id,
                "task_id": task_id,
                "decision": decision,
                "principal": principal,
                "scope": scope,
                "projection_hash": before.projection_hash,
                "journal_head": before.journal_head,
                "journal_entry_hash": journal_entry_hash,
                "diff_sha256": diff_hash,
                "audit_event_hash": audit_event_hash,
            },
            "board": after.to_dict() if after is not None else None,
        }
    )


# ---------------------------------------------------------------------------
# Board page (vanilla JS; zero client-side state beyond the last fetch)
# ---------------------------------------------------------------------------

_COLUMN_TITLES: Final[dict[str, str]] = {
    "queued": "Queued",
    "running": "Running",
    "gated": "Gated",
    "needs_review": "Needs review",
    "merged": "Merged",
}

_BOARD_CSS: Final[str] = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0d1117; color: #e6edf3;
       font: 14px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif; }
header { display: flex; gap: 12px; align-items: center; padding: 10px 16px;
         border-bottom: 1px solid #21262d; flex-wrap: wrap; }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header select { background: #161b22; color: inherit; border: 1px solid #30363d;
                border-radius: 6px; padding: 4px 8px; }
#receipt { margin-left: auto; font-size: 12px; color: #8b949e;
           font-family: ui-monospace, monospace; }
#receipt .ok { color: #3fb950; }
#receipt .bad { color: #f85149; font-weight: 700; }
main { display: grid; grid-template-columns: repeat(5, minmax(180px, 1fr));
       gap: 10px; padding: 12px 16px; align-items: start; }
.col { background: #161b22; border: 1px solid #21262d; border-radius: 8px;
       padding: 8px; min-height: 120px; }
.col h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
          color: #8b949e; margin: 2px 4px 8px; }
.col h2 .count { color: #e6edf3; }
.card { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
        padding: 8px; margin-bottom: 8px; cursor: pointer; }
.card:hover { border-color: #58a6ff; }
.card .tid { font-family: ui-monospace, monospace; font-size: 12px; }
.card .meta { color: #8b949e; font-size: 12px; margin-top: 4px;
              overflow-wrap: anywhere; }
.card .gate { color: #f0883e; font-size: 12px; margin-top: 4px; }
#drawer { position: fixed; top: 0; right: 0; width: min(440px, 90vw);
          height: 100vh; background: #161b22; border-left: 1px solid #30363d;
          padding: 16px; overflow-y: auto; display: none; }
#drawer.open { display: block; }
#drawer h3 { margin: 0 0 8px; font-size: 14px;
             font-family: ui-monospace, monospace; }
#drawer pre { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
              padding: 8px; font-size: 12px; white-space: pre-wrap;
              overflow-wrap: anywhere; }
#drawer button { background: #21262d; color: inherit; border: 1px solid #30363d;
                 border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.badge { display: inline-block; border-radius: 10px; padding: 1px 8px;
         font-size: 11px; border: 1px solid #30363d; margin-right: 6px; }
.badge.pass { color: #3fb950; border-color: #3fb950; }
.badge.fail { color: #f85149; border-color: #f85149; }
.card .review { font-size: 12px; margin-top: 4px; }
.card .review.approve { color: #3fb950; }
.card .review.request_changes { color: #f0883e; }
#drawer .diff-controls { margin: 4px 0 6px; }
#drawer .diff-controls button { margin-right: 6px; font-size: 11px; }
.diff-file { border: 1px solid #21262d; border-radius: 6px; margin-bottom: 6px; }
.diff-file > summary { cursor: pointer; padding: 5px 8px; font-family: ui-monospace, monospace;
                       font-size: 12px; list-style: none; }
.diff-file > summary::-webkit-details-marker { display: none; }
.diff-file > summary .fold { color: #8b949e; margin-right: 6px; }
.diff-file .add { color: #3fb950; } .diff-file .del { color: #f85149; }
.diff-hunk { border-top: 1px solid #21262d; }
.diff-hunk > summary { cursor: pointer; padding: 3px 8px 3px 20px; color: #8b949e;
                       font-family: ui-monospace, monospace; font-size: 12px; list-style: none; }
.diff-hunk > summary::-webkit-details-marker { display: none; }
.diff-body { margin: 0; padding: 4px 8px; font-family: ui-monospace, monospace; font-size: 12px;
             white-space: pre-wrap; overflow-wrap: anywhere; }
.diff-body .l { display: block; }
.diff-body .l.add { color: #3fb950; background: rgba(63,185,80,.08); }
.diff-body .l.del { color: #f85149; background: rgba(248,81,73,.08); }
.diff-body .l.at { color: #8b949e; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0; }
.actions button { flex: 1; min-width: 96px; }
.actions button.approve { border-color: #3fb950; color: #3fb950; }
.actions button.request_changes { border-color: #f0883e; color: #f0883e; }
.actions button.merge { border-color: #58a6ff; color: #58a6ff; }
.actions button:disabled { opacity: .5; cursor: default; }
#drawer-receipt { font-size: 12px; font-family: ui-monospace, monospace;
                  overflow-wrap: anywhere; margin-top: 6px; }
#drawer-receipt .ok { color: #3fb950; } #drawer-receipt .bad { color: #f85149; }
@media (max-width: 900px) { main { grid-template-columns: 1fr 1fr; } }
"""

_BOARD_JS: Final[str] = """
'use strict';
(function () {
  var state = { runId: null };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function api(path) {
    return fetch(path, { headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error(path + ' -> HTTP ' + r.status);
      return r.json();
    });
  }

  function renderReceipt(p) {
    var box = document.getElementById('receipt');
    box.textContent = '';
    var verified = el('span', p.journal_verified ? 'ok' : 'bad',
      p.journal_verified ? 'journal verified' : 'JOURNAL TAMPERED');
    box.appendChild(verified);
    box.appendChild(el('span', '', ' | projection ' + p.projection_hash.slice(0, 16)));
    box.appendChild(el('span', '', ' | head ' + (p.journal_head || 'genesis').slice(0, 16)));
    box.appendChild(el('span', '', ' | events ' + p.event_count));
  }

  function renderBoard(p) {
    renderReceipt(p);
    Object.keys(p.board.columns).forEach(function (column) {
      var host = document.querySelector('[data-column="' + column + '"]');
      if (!host) return;
      var list = host.querySelector('.cards');
      list.textContent = '';
      host.querySelector('.count').textContent = String(p.board.columns[column].length);
      p.board.columns[column].forEach(function (card) {
        var node = el('div', 'card');
        node.appendChild(el('div', 'tid', card.task_id));
        var meta = [];
        if (card.agent_id) meta.push(card.agent_id);
        if (card.model) meta.push(card.model);
        if (card.attempts > 1) meta.push('attempt ' + card.attempts);
        if (card.cost_usd !== null && card.cost_usd !== undefined) meta.push('$' + card.cost_usd);
        if (meta.length) node.appendChild(el('div', 'meta', meta.join(' | ')));
        if (card.gate_failures.length) {
          node.appendChild(el('div', 'gate', 'gates: ' + card.gate_failures.join(', ')));
        }
        if (card.diff) {
          node.appendChild(el('div', 'meta',
            'diff +' + card.diff.added + '/-' + card.diff.removed + ' (' + card.diff.files + ' files)'));
        }
        if (card.review) {
          node.appendChild(el('div', 'review ' + card.review.decision,
            String(card.review.decision).replace('_', ' ') + ' by ' + (card.review.principal || 'operator')));
        }
        node.addEventListener('click', function () { openDrawer(card); });
        list.appendChild(node);
      });
    });
  }

  // Port of the TUI diff folding: parse a unified diff into files -> hunks
  // and render each file/hunk as a collapsible <details>. Diff text is only
  // ever assigned via textContent, never innerHTML, so file contents cannot
  // inject markup into the page.
  function parseDiff(text) {
    var files = [], file = null, hunk = null;
    text.split('\\n').forEach(function (line) {
      var m = /^diff --git a\\/(.+?) b\\/(.+)$/.exec(line);
      if (m) { file = { name: m[2], hunks: [], added: 0, removed: 0 }; files.push(file); hunk = null; return; }
      if (!file) return;
      if (line.indexOf('@@') === 0) { hunk = { header: line, lines: [line] }; file.hunks.push(hunk); return; }
      if (hunk) hunk.lines.push(line);
      if (line[0] === '+' && line.indexOf('+++') !== 0) file.added++;
      else if (line[0] === '-' && line.indexOf('---') !== 0) file.removed++;
    });
    return files;
  }

  function lineClass(line) {
    if (line.indexOf('@@') === 0) return 'l at';
    if (line[0] === '+' && line.indexOf('+++') !== 0) return 'l add';
    if (line[0] === '-' && line.indexOf('---') !== 0) return 'l del';
    return 'l';
  }

  function renderDiff(host, data) {
    host.textContent = '';
    var head = el('div', 'diff-controls');
    var verified = el('span', 'badge ' + (data.verified ? 'pass' : 'fail'),
      data.verified ? 'diff verified' : 'DIFF UNVERIFIED');
    head.appendChild(verified);
    head.appendChild(el('span', 'meta', ' ' + data.sha256.slice(0, 23)));
    var expand = el('button', '', 'expand all');
    var collapse = el('button', '', 'collapse all');
    head.appendChild(expand); head.appendChild(collapse);
    host.appendChild(head);

    var files = parseDiff(data.diff_text || '');
    if (!files.length) { host.appendChild(el('div', 'meta', 'no textual diff captured')); return; }
    files.forEach(function (file) {
      var fileEl = document.createElement('details');
      fileEl.className = 'diff-file';
      var summary = document.createElement('summary');
      summary.appendChild(el('span', 'fold', '\\u25b8'));
      summary.appendChild(document.createTextNode(file.name + ' '));
      summary.appendChild(el('span', 'add', '+' + file.added));
      summary.appendChild(document.createTextNode('/'));
      summary.appendChild(el('span', 'del', '-' + file.removed));
      fileEl.appendChild(summary);
      file.hunks.forEach(function (h) {
        var hunkEl = document.createElement('details');
        hunkEl.className = 'diff-hunk';
        hunkEl.open = true;
        var hs = document.createElement('summary');
        hs.textContent = h.header;
        hunkEl.appendChild(hs);
        var body = el('div', 'diff-body');
        h.lines.slice(1).forEach(function (line) {
          body.appendChild(el('span', lineClass(line), line));
        });
        hunkEl.appendChild(body);
        fileEl.appendChild(hunkEl);
      });
      host.appendChild(fileEl);
    });
    expand.addEventListener('click', function () {
      host.querySelectorAll('details').forEach(function (d) { d.open = true; });
    });
    collapse.addEventListener('click', function () {
      host.querySelectorAll('.diff-file').forEach(function (d) { d.open = false; });
    });
  }

  function openDrawer(card) {
    var drawer = document.getElementById('drawer');
    drawer.classList.add('open');
    state.task = card.task_id;
    document.getElementById('drawer-task').textContent = card.task_id;
    document.getElementById('drawer-receipt').textContent = '';
    var gates = document.getElementById('drawer-gates');
    gates.textContent = card.gate_failures.length
      ? card.gate_failures.join('\\n') : 'no gate failures recorded';

    var diffHost = document.getElementById('drawer-diff');
    diffHost.textContent = 'loading diff...';
    api('/review-board/runs/' + encodeURIComponent(state.runId) +
        '/diff/' + encodeURIComponent(card.task_id))
      .then(function (data) { renderDiff(diffHost, data); })
      .catch(function () { diffHost.textContent = 'no diff captured for this task'; });

    var evidence = document.getElementById('drawer-evidence');
    evidence.textContent = 'loading evidence bundle...';
    api('/review-board/runs/' + encodeURIComponent(state.runId) +
        '/evidence/' + encodeURIComponent(card.task_id))
      .then(function (bundle) {
        evidence.textContent = '';
        var badge = el('span', 'badge ' + (bundle.gate_passed ? 'pass' : 'fail'),
          bundle.gate_passed ? 'gate passed' : 'gate failed');
        evidence.appendChild(badge);
        evidence.appendChild(el('span', 'meta', 'bundle ' + bundle.bundle_hash.slice(0, 23)));
        (bundle.items || []).forEach(function (item) {
          var row = el('pre', '',
            item.name + ' [' + item.kind + '] ' + item.status +
            ' exit=' + item.exit_code + '\\n' + item.content_hash);
          evidence.appendChild(row);
        });
      })
      .catch(function () {
        evidence.textContent = 'no evidence bundle sealed for this task';
      });
  }

  // Board actions are governance receipts: POST to the dashboard-scoped
  // action endpoint, which appends the decision to the run journal and mirrors
  // a signed audit entry naming the acting principal, then re-renders whatever
  // the returned projection says.
  function postAction(decision) {
    if (!state.runId || !state.task) return;
    var buttons = document.querySelectorAll('.actions button');
    buttons.forEach(function (b) { b.disabled = true; });
    var out = document.getElementById('drawer-receipt');
    out.textContent = 'recording ' + decision + '...';
    fetch('/dashboard/review-board/runs/' + encodeURIComponent(state.runId) +
          '/tasks/' + encodeURIComponent(state.task) + '/review',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ decision: decision })
      })
      .then(function (r) {
        if (r.ok) return r.json();
        return r.json().then(function (e) { throw new Error(e.detail || r.status); });
      })
      .then(function (body) {
        var receipt = body.receipt;
        out.textContent = '';
        out.appendChild(el('div', 'ok', receipt.decision + ' recorded by ' + receipt.principal));
        out.appendChild(el('div', '', 'journal entry ' + receipt.journal_entry_hash.slice(0, 16)));
        if (receipt.audit_event_hash) {
          out.appendChild(el('div', '', 'signed receipt ' + receipt.audit_event_hash.slice(0, 16)));
        }
        buttons.forEach(function (b) { b.disabled = false; });
        if (body.board) renderBoard(body.board);
      })
      .catch(function (err) {
        out.textContent = '';
        out.appendChild(el('div', 'bad', 'action failed: ' + String(err.message || err)));
        buttons.forEach(function (b) { b.disabled = false; });
      });
  }

  function refresh() {
    if (!state.runId) return;
    api('/review-board/runs/' + encodeURIComponent(state.runId))
      .then(renderBoard)
      .catch(function (err) {
        document.getElementById('receipt').textContent = String(err);
      });
  }

  function loadRuns() {
    api('/review-board/runs').then(function (body) {
      var picker = document.getElementById('run-picker');
      picker.textContent = '';
      body.runs.forEach(function (runId) {
        var option = document.createElement('option');
        option.value = runId;
        option.textContent = runId;
        picker.appendChild(option);
      });
      if (body.runs.length) {
        state.runId = body.runs[0];
        picker.value = state.runId;
        refresh();
      } else {
        document.getElementById('receipt').textContent = 'no recorded runs';
      }
    });
  }

  document.getElementById('run-picker').addEventListener('change', function (event) {
    state.runId = event.target.value;
    document.getElementById('drawer').classList.remove('open');
    refresh();
  });
  document.getElementById('drawer-close').addEventListener('click', function () {
    document.getElementById('drawer').classList.remove('open');
  });
  document.querySelectorAll('.actions button').forEach(function (btn) {
    btn.addEventListener('click', function () { postAction(btn.getAttribute('data-decision')); });
  });

  // Live runs: any task/agent update on the existing SSE stream triggers a
  // re-projection. The client keeps no board state - the server re-folds
  // the journal and the page re-renders whatever comes back. Detached runs
  // simply never fire events, so the board stays a static projection.
  try {
    var source = new EventSource('/events');
    ['task_update', 'agent_update', 'task.created', 'task.completed',
     'gate.result', 'merge.completed'].forEach(function (eventType) {
      source.addEventListener(eventType, refresh);
    });
  } catch (err) { /* SSE unavailable: manual reload still works */ }

  loadRuns();
})();
"""


def _board_page_html() -> str:
    """Assemble the review-board page (no external assets, no build step)."""
    columns = "\n".join(
        f'      <section class="col" data-column="{column}">'
        f'<h2>{_COLUMN_TITLES[column]} <span class="count">0</span></h2>'
        f'<div class="cards"></div></section>'
        for column in BOARD_COLUMNS
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bernstein - Run review board</title>
  <style>{_BOARD_CSS}</style>
</head>
<body>
  <header>
    <h1>Run review board</h1>
    <select id="run-picker" aria-label="run"></select>
    <span id="receipt"></span>
  </header>
  <main>
{columns}
  </main>
  <aside id="drawer">
    <button id="drawer-close" type="button">close</button>
    <h3 id="drawer-task"></h3>
    <h4>Diff</h4>
    <div id="drawer-diff"></div>
    <h4>Gate receipts</h4>
    <pre id="drawer-gates"></pre>
    <h4>Evidence bundle</h4>
    <div id="drawer-evidence"></div>
    <h4>Review</h4>
    <div class="actions">
      <button type="button" class="approve" data-decision="approve">Approve</button>
      <button type="button" class="request_changes" data-decision="request_changes">Request changes</button>
      <button type="button" class="merge" data-decision="merge">Merge</button>
    </div>
    <div id="drawer-receipt"></div>
  </aside>
  <script>{_BOARD_JS}</script>
</body>
</html>
"""


@router.get("/dashboard/review-board", response_class=HTMLResponse)
def review_board_page() -> HTMLResponse:
    """Serve the review-board page.

    The page is a pure consumer of the projection endpoints above plus the
    existing ``/events`` SSE stream; it holds no state of its own, so
    reloading it (or opening it on a second machine against the same
    journal) renders the identical board.
    """
    return HTMLResponse(content=_board_page_html())
