"""Sandbox evaluation routes.

Exposes endpoints for prospects to create sandboxed sessions:
paste a GitHub URL, pick a solution pack, and watch agents work.
"""

from __future__ import annotations

import html
import json
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Session IDs are server-generated hex UUIDs / short hex tokens. Anything
# outside this character class must be rejected before being embedded in
# HTML or JS template strings to prevent XSS (SonarCloud S5131).
_SESSION_ID_ALLOWED = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_session_id(session_id: str) -> str:
    """Return ``session_id`` if it matches the allowlist, else raise 400.

    Used as an XSS guard before embedding session_id into HTML or JS
    string literals in the sandbox dashboard template.
    """
    if not _SESSION_ID_ALLOWED.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    return session_id


if TYPE_CHECKING:
    from bernstein.core.sandbox_eval import SandboxManager

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


def _get_manager(request: Request) -> SandboxManager:
    mgr: SandboxManager | None = getattr(request.app.state, "sandbox_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Sandbox not enabled on this instance")
    return mgr


class CreateSessionRequest(BaseModel):
    repo_url: str
    solution_pack: str = "code-quality"


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str
    dashboard_url: str


class SessionResponse(BaseModel):
    id: str
    repo_url: str
    solution_pack: str
    status: str
    created_at: float
    started_at: float
    finished_at: float
    budget_used_usd: float
    budget_limit_usd: float
    agents_spawned: int
    max_agents: int
    task_ids: list[str]
    elapsed_s: float
    error: str


class SolutionPackInfo(BaseModel):
    id: str
    name: str
    goal: str
    team: list[str]
    max_agents: int
    estimated_minutes: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/packs", response_model=list[SolutionPackInfo], responses={503: {"description": "Sandbox not enabled"}})
async def list_solution_packs(request: Request) -> list[dict[str, Any]]:
    """List available solution packs."""
    mgr = _get_manager(request)
    return mgr.get_solution_packs()


@router.post(
    "/sessions",
    response_model=CreateSessionResponse,
    status_code=201,
    responses={
        400: {"description": "Invalid request parameters"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Sandbox not enabled"},
    },
)
async def create_session(body: CreateSessionRequest, request: Request) -> dict[str, Any]:
    """Create a new sandbox evaluation session."""
    mgr = _get_manager(request)
    client_ip = request.client.host if request.client else ""

    try:
        session = mgr.create_session(body.repo_url, body.solution_pack, client_ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None

    return {
        "session_id": session.id,
        "status": session.status.value,
        "dashboard_url": f"/sandbox/{session.id}",
    }


@router.get("/sessions", response_model=list[SessionResponse], responses={503: {"description": "Sandbox not enabled"}})
async def list_sessions(request: Request, include_finished: bool = False) -> list[dict[str, Any]]:
    """List sandbox sessions."""
    mgr = _get_manager(request)
    return mgr.list_sessions(include_finished=include_finished)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    responses={404: {"description": "Session not found"}, 503: {"description": "Sandbox not enabled"}},
)
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    """Get sandbox session details."""
    mgr = _get_manager(request)
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@router.post(
    "/sessions/{session_id}/cancel",
    status_code=200,
    responses={
        404: {"description": "Session not found or already finished"},
        503: {"description": "Sandbox not enabled"},
    },
)
async def cancel_session(session_id: str, request: Request) -> dict[str, str]:
    """Cancel a running sandbox session."""
    mgr = _get_manager(request)
    if not mgr.cancel(session_id):
        raise HTTPException(status_code=404, detail="Session not found or already finished")
    return {"status": "cancelled"}


@router.get(
    "/{session_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    responses={
        400: {"description": "Invalid session id"},
        404: {"description": "Session not found"},
        503: {"description": "Sandbox not enabled"},
    },
)
async def sandbox_dashboard(session_id: str, request: Request) -> HTMLResponse:
    """Serve the sandbox session dashboard page."""
    session_id = _validate_session_id(session_id)
    mgr = _get_manager(request)
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # html.escape at the call site so CodeQL's taint tracker sees the
    # sanitizer on the same data-flow edge as the HTMLResponse sink
    # (it does not look inside _render_sandbox_page for internal escaping).
    safe_id = html.escape(session_id)
    return HTMLResponse(_render_sandbox_page(safe_id))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def sandbox_landing(request: Request) -> HTMLResponse:
    """Landing page for sandbox evaluation."""
    _get_manager(request)  # ensure sandbox is enabled
    return HTMLResponse(_render_landing_page())


# ---------------------------------------------------------------------------
# HTML templates (inline — no Jinja dependency)
# ---------------------------------------------------------------------------

_COMMON_HEAD = """\
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"></script>
"""


def _render_landing_page() -> str:
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <title>Bernstein — Try It Free</title>
  {_COMMON_HEAD}
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen">
  <div class="max-w-2xl mx-auto px-6 py-16" x-data="sandbox()">
    <h1 class="text-3xl font-bold mb-2">Try Bernstein</h1>
    <p class="text-gray-400 mb-8">
      Paste a public GitHub repo URL, pick what you want agents to do,
      and watch them work. 3 agents, $2 budget, no install required.
    </p>

    <form @submit.prevent="submit" class="space-y-6">
      <div>
        <label class="block text-sm font-medium mb-1">GitHub Repository URL</label>
        <input
          type="url" x-model="repoUrl" required
          placeholder="https://github.com/owner/repo"
          class="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2
                 focus:border-blue-500 focus:outline-none"
        >
      </div>

      <div>
        <label class="block text-sm font-medium mb-2">Solution Pack</label>
        <div class="grid grid-cols-2 gap-3">
          <template x-for="pack in packs" :key="pack.id">
            <button type="button"
              @click="selectedPack = pack.id"
              :class="selectedPack === pack.id
                ? 'border-blue-500 bg-blue-500/10'
                : 'border-gray-700 hover:border-gray-500'"
              class="border rounded p-3 text-left transition">
              <div class="font-medium text-sm" x-text="pack.name"></div>
              <div class="text-xs text-gray-400 mt-1" x-text="pack.goal"></div>
              <div class="text-xs text-gray-500 mt-1">
                <span x-text="pack.max_agents"></span> agents &middot;
                ~<span x-text="pack.estimated_minutes"></span> min
              </div>
            </button>
          </template>
        </div>
      </div>

      <button type="submit" :disabled="loading"
        class="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700
               rounded py-2 font-medium transition">
        <span x-show="!loading">Start Evaluation</span>
        <span x-show="loading">Starting...</span>
      </button>

      <p x-show="error" class="text-red-400 text-sm" x-text="error"></p>
    </form>

    <p class="text-gray-600 text-xs mt-8">
      Public repos only. Sessions expire after 30 minutes.
      Limited to 3 concurrent sessions per IP.
    </p>
  </div>

  <script>
  function sandbox() {{
    return {{
      repoUrl: '',
      selectedPack: 'code-quality',
      loading: false,
      error: '',
      packs: [],
      async init() {{
        const resp = await fetch('/sandbox/packs');
        this.packs = await resp.json();
      }},
      async submit() {{
        this.loading = true;
        this.error = '';
        try {{
          const resp = await fetch('/sandbox/sessions', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
              repo_url: this.repoUrl,
              solution_pack: this.selectedPack,
            }}),
          }});
          if (!resp.ok) {{
            const data = await resp.json();
            this.error = data.detail || 'Failed to create session';
            return;
          }}
          const data = await resp.json();
          window.location.href = data.dashboard_url;
        }} catch (e) {{
          this.error = 'Network error — please try again';
        }} finally {{
          this.loading = false;
        }}
      }},
    }};
  }}
  </script>
</body>
</html>"""


def _render_sandbox_page(session_id: str) -> str:
    # Defence in depth: even though ``_validate_session_id`` already
    # restricts session_id to ``[A-Za-z0-9_-]{1,64}``, escape the value
    # before embedding into HTML (html.escape for the <title>) and into
    # JS string literals (json.dumps to get a safely quoted literal). This
    # also satisfies CodeQL py/reflective-xss which does not recognise
    # the regex allowlist as a sanitiser.
    safe_title_id = html.escape(session_id)
    safe_js_id = json.dumps(session_id)
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <title>Bernstein Sandbox — Session {safe_title_id}</title>
  {_COMMON_HEAD}
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen">
  <div class="max-w-4xl mx-auto px-6 py-8" x-data="session()" x-init="poll()">
    <div class="flex items-center justify-between mb-6">
      <div>
        <a href="/sandbox/" class="text-gray-500 text-sm hover:text-gray-300">&larr; Back</a>
        <h1 class="text-2xl font-bold mt-1">Sandbox Session</h1>
      </div>
      <span :class="statusColor" class="px-3 py-1 rounded-full text-sm font-medium"
            x-text="data.status || 'loading'"></span>
    </div>

    <template x-if="data.id">
      <div class="space-y-6">
        <div class="grid grid-cols-3 gap-4">
          <div class="bg-gray-900 rounded p-4">
            <div class="text-gray-500 text-xs uppercase">Budget</div>
            <div class="text-xl font-bold mt-1">
              $<span x-text="data.budget_used_usd.toFixed(2)"></span>
              <span class="text-gray-500 text-sm">/ $<span x-text="data.budget_limit_usd.toFixed(2)"></span></span>
            </div>
            <div class="w-full bg-gray-800 rounded-full h-2 mt-2">
              <div class="bg-blue-500 h-2 rounded-full transition-all"
                   :style="'width:' + Math.min(100, data.budget_used_usd / data.budget_limit_usd * 100) + '%'"></div>
            </div>
          </div>
          <div class="bg-gray-900 rounded p-4">
            <div class="text-gray-500 text-xs uppercase">Agents</div>
            <div class="text-xl font-bold mt-1">
              <span x-text="data.agents_spawned"></span>
              <span class="text-gray-500 text-sm">/ <span x-text="data.max_agents"></span></span>
            </div>
          </div>
          <div class="bg-gray-900 rounded p-4">
            <div class="text-gray-500 text-xs uppercase">Elapsed</div>
            <div class="text-xl font-bold mt-1" x-text="formatElapsed(data.elapsed_s)"></div>
          </div>
        </div>

        <div class="bg-gray-900 rounded p-4">
          <div class="text-gray-500 text-xs uppercase mb-2">Details</div>
          <div class="grid grid-cols-2 gap-2 text-sm">
            <div class="text-gray-400">Repository</div>
            <div><a :href="data.repo_url" class="text-blue-400 hover:underline"
              x-text="data.repo_url" target="_blank"></a></div>
            <div class="text-gray-400">Solution Pack</div>
            <div x-text="data.solution_pack"></div>
            <template x-if="data.error">
              <div class="col-span-2 text-red-400 mt-2" x-text="data.error"></div>
            </template>
          </div>
        </div>

        <div x-show="data.task_ids.length > 0" class="bg-gray-900 rounded p-4">
          <div class="text-gray-500 text-xs uppercase mb-2">Tasks</div>
          <div class="space-y-1">
            <template x-for="tid in data.task_ids" :key="tid">
              <div class="text-sm font-mono text-gray-300">
                <a :href="'/dashboard/tasks/' + tid" class="hover:text-blue-400" x-text="tid"></a>
              </div>
            </template>
          </div>
        </div>

        <div class="flex gap-3" x-show="!isTerminal">
          <button @click="cancel()"
            class="bg-red-600/20 text-red-400 hover:bg-red-600/30 rounded px-4 py-2 text-sm transition">
            Cancel Session
          </button>
        </div>
      </div>
    </template>
  </div>

  <script>
  function session() {{
    return {{
      data: {{}},
      get isTerminal() {{
        return ['completed','failed','timed_out','cancelled'].includes(this.data.status);
      }},
      get statusColor() {{
        const m = {{
          queued: 'bg-gray-700 text-gray-300',
          cloning: 'bg-yellow-900 text-yellow-300',
          running: 'bg-blue-900 text-blue-300',
          completed: 'bg-green-900 text-green-300',
          failed: 'bg-red-900 text-red-300',
          timed_out: 'bg-orange-900 text-orange-300',
          cancelled: 'bg-gray-700 text-gray-400',
        }};
        return m[this.data.status] || 'bg-gray-700';
      }},
      formatElapsed(s) {{
        if (!s) return '0s';
        const m = Math.floor(s / 60);
        const sec = Math.floor(s % 60);
        return m > 0 ? m + 'm ' + sec + 's' : sec + 's';
      }},
      async poll() {{
        await this.refresh();
        if (!this.isTerminal) {{
          setTimeout(() => this.poll(), 3000);
        }}
      }},
      async refresh() {{
        try {{
          const resp = await fetch('/sandbox/sessions/' + {safe_js_id});
          if (resp.ok) this.data = await resp.json();
        }} catch (e) {{ /* retry on next poll */ }}
      }},
      async cancel() {{
        await fetch('/sandbox/sessions/' + {safe_js_id} + '/cancel', {{method: 'POST'}});
        await this.refresh();
      }},
    }};
  }}
  </script>
</body>
</html>"""
