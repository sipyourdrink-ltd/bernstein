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
* ``GET /dashboard/review-board`` - dependency-free HTML board page. Also
  served by ``bernstein gui serve`` (which mounts this same app), and
  refreshed from the existing ``/events`` SSE stream.

Bearer auth: the task server's auth middleware covers every route here
(none is registered as a public path). Scoped browser tokens for
non-loopback deployments arrive with the dashboard-auth work and plug in
front of this surface unchanged.

Deliberate non-goals (board scope): no editing, no chat, no board-side
state, no write endpoints. Approve / request-changes / merge actions stay
on the attested-approval path and land as a follow-up slice.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bernstein.core.evidence.bundle import read_evidence_bundle
from bernstein.core.replay.review_board import (
    BOARD_COLUMNS,
    list_board_runs,
    project_run,
)

router = APIRouter()

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
        node.addEventListener('click', function () { openDrawer(card); });
        list.appendChild(node);
      });
    });
  }

  function openDrawer(card) {
    var drawer = document.getElementById('drawer');
    drawer.classList.add('open');
    var title = document.getElementById('drawer-task');
    title.textContent = card.task_id;
    var gates = document.getElementById('drawer-gates');
    gates.textContent = card.gate_failures.length
      ? card.gate_failures.join('\\n') : 'no gate failures recorded';
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
    <h4>Gate receipts</h4>
    <pre id="drawer-gates"></pre>
    <h4>Evidence bundle</h4>
    <div id="drawer-evidence"></div>
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
