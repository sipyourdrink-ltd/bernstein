// Single fetch wrapper for /api/v1/* - bearer auth from localStorage.
//
// Path-resolution contract (FE-PROXY-001):
//   The Bernstein backend mounts every router twice - at `/<path>` AND at
//   `/api/v1/<path>` (see ``src/bernstein/core/server/server_app.py``'s
//   ``all_routers`` loop). In production the SPA is served by the backend so
//   both shapes resolve. In dev the SPA is served by Vite on a different port,
//   and ``vite.config.ts`` only proxies the ``/api`` prefix to the backend -
//   so the frontend MUST send every request under ``/api/v1`` for it to
//   reach the orchestrator.
//
//   To keep the call sites readable (`apiGet('/agents')` rather than
//   `apiGet('/api/v1/agents')`) and to make accidental double-prefixing
//   harmless when a caller copies an SSE-shaped URL into apiGet, this wrapper:
//     - prepends ``BASE`` to bare paths;
//     - returns an already-prefixed ``/api/v1/...`` path unchanged
//       (idempotent);
//     - passes through absolute ``http(s)://`` URLs untouched.
//
//   Smoke check (no framework configured, so this stays as a doc-test):
//     buildUrl('/agents')                === '/api/v1/agents'
//     buildUrl('/api/v1/agents')         === '/api/v1/agents'
//     buildUrl('/audit/verify')          === '/api/v1/audit/verify'
//     buildUrl('https://x.test/y')       === 'https://x.test/y'
//     buildUrl('/api/v1/dashboard/x')    === '/api/v1/dashboard/x'
//     buildUrl('')                       === '/api/v1'
//
// SSE endpoints constructed in ``web/src/lib/sse.ts`` go through
// ``new EventSource(url)`` directly - those callers pass fully-qualified
// ``/api/v1/...`` URLs because EventSource has no shared base.

const TOKEN_KEY = 'bernstein_token';
const BASE = '/api/v1';

/**
 * Resolve a caller-supplied path to a concrete URL. Idempotent: passing an
 * already-prefixed ``/api/v1/...`` path is a no-op so accidental double-
 * prefixing in a future caller can't silently produce ``/api/v1/api/v1/...``.
 *
 * Exported for ergonomics (e.g. building blob-download URLs that bypass the
 * JSON wrapper but still need the same base resolution).
 */
export function buildUrl(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) return path;
  if (path === BASE || path.startsWith(`${BASE}/`)) return path;
  return `${BASE}${path}`;
}

export class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
    // Preserve a useful name for instanceof and devtools display.
    this.name = 'ApiError';
  }
}

function authHeaders(): HeadersInit {
  const token = typeof window !== 'undefined' ? window.localStorage.getItem(TOKEN_KEY) : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Best-effort body parse for both error and success paths.
 * - JSON content-type → parsed object
 * - Anything else with a body → trimmed text
 * - Empty body → null
 * Never throws; returns null on parse failure.
 */
async function readBody(r: Response): Promise<unknown> {
  const ct = r.headers.get('content-type') ?? '';
  try {
    const text = await r.text();
    if (!text) return null;
    if (ct.includes('application/json')) {
      try {
        return JSON.parse(text);
      } catch {
        // Backend lied about content-type - keep raw text so callers can debug.
        return text;
      }
    }
    return text;
  } catch {
    return null;
  }
}

export async function api<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const url = buildUrl(path);
  // Only set Content-Type when we actually send a body - some servers reject
  // GET/DELETE with a content-type header, and 204/202 responses confuse CORS preflights.
  const hasBody = init.body != null;
  const baseHeaders: Record<string, string> = { Accept: 'application/json' };
  if (hasBody) baseHeaders['Content-Type'] = 'application/json';
  const r = await fetch(url, {
    ...init,
    headers: {
      ...baseHeaders,
      ...authHeaders(),
      ...(init.headers ?? {}),
    },
  });
  if (r.status === 401) {
    if (typeof window !== 'undefined') window.localStorage.removeItem(TOKEN_KEY);
    // Preserve any server-provided error body so callers can surface a reason.
    const body = await readBody(r);
    throw new ApiError(401, body, 'Unauthorized - clear session and re-auth');
  }
  if (!r.ok) {
    const body = await readBody(r);
    throw new ApiError(r.status, body, `${r.status} ${r.statusText} - ${path}`);
  }
  // 204 No Content / 205 Reset Content / explicit empty body.
  if (r.status === 204 || r.status === 205) return undefined as T;
  const ct = r.headers.get('content-type') ?? '';
  // JSON path: parse and return.
  if (ct.includes('application/json')) {
    const text = await r.text();
    if (!text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch (err) {
      throw new ApiError(
        r.status,
        text,
        `${path} - server claimed application/json but returned unparseable body: ${(err as Error).message}`,
      );
    }
  }
  // Unexpected non-JSON success body. Don't silently return undefined: surface a
  // useful error so callers know the contract was violated (e.g. backend served
  // an HTML 200 from a misconfigured proxy). Empty bodies remain undefined.
  const text = await r.text();
  if (!text) return undefined as T;
  throw new ApiError(
    r.status,
    text,
    `${path} - expected JSON but got ${ct || 'unknown content-type'}`,
  );
}

export const apiGet = <T = unknown>(path: string, init?: RequestInit) =>
  api<T>(path, { ...init, method: 'GET' });
export const apiPost = <T = unknown>(path: string, body?: unknown, init?: RequestInit) =>
  api<T>(path, { ...init, method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined });
export const apiPut = <T = unknown>(path: string, body?: unknown, init?: RequestInit) =>
  api<T>(path, { ...init, method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined });
export const apiDelete = <T = unknown>(path: string, init?: RequestInit) =>
  api<T>(path, { ...init, method: 'DELETE' });

// ── Fleet steering (#2508) ──────────────────────────────────────────────────
// Operator-outbound mid-task control. Every action is a receipt first and an
// effect second: the server binds the command into the audit chain before the
// effect runs and returns the receipt the delivered effect references. Mirrors
// `TaskSteerPost` / `TaskSteerResponse` in
// `src/bernstein/core/server/server_models.py`.

export type SteerKind = 'pause' | 'resume' | 'guidance' | 'redirect' | 'abort';

export interface SteerCommand {
  kind: SteerKind;
  principal?: string;
  guidance?: string;
  redirect_target?: string;
  reason?: string;
  session_id?: string;
  adapter?: string;
  worktree?: string;
  displayed_payload_hash?: string | null;
}

export interface SteerReceipt {
  kind: string;
  task_id: string;
  principal: string;
  scope: string;
  payload_hash: string;
  receipt_hash: string;
  timestamp: number;
  mailbox_seq: number;
  mailbox_entry_hash: string;
  checkpoint_event_hash: string;
  abort_signal_written: boolean;
}

/** Steer one running worker; resolves to the signed receipt. */
export function steerTask(taskId: string, command: SteerCommand): Promise<SteerReceipt> {
  return apiPost<SteerReceipt>(`/tasks/${encodeURIComponent(taskId)}/steer`, command);
}

// ── Mission timeline (#2510) ────────────────────────────────────────────────
// Outcome-level view over a multi-day mission. The server folds the mission's
// work-ledger chain on every request; the client renders whatever the fold
// returns. Every element carries the receipt / evidence handle it was derived
// from, and `ledger_verified === false` flips the screen to an unverified
// banner. Mirrors `MissionProjection.to_dict()` /
// `MissionStatus.to_dict()` in
// `src/bernstein/core/orchestration/missions.py` and the routes in
// `src/bernstein/core/routes/missions.py`.

export type PhaseState =
  | 'pending'
  | 'active'
  | 'passed'
  | 'halted'
  | 'unverified';

export type MissionOverall =
  | 'pending'
  | 'active'
  | 'complete'
  | 'halted'
  | 'unverified';

export interface MissionPhaseStatus {
  phase_id: string;
  name: string;
  state: PhaseState | string;
  gate: string[];
  gate_passed: boolean;
  evidence_bundle_hashes: string[];
  envelope: string;
  budget_usd: number;
  spend_usd: number;
  receipt_hash: string;
  ledger_seq: number;
}

export interface MissionStatus {
  schema_version: number;
  mission_id: string;
  goal_digest: string;
  spec_hash: string;
  phases: MissionPhaseStatus[];
  active_phase: string;
  overall: MissionOverall | string;
}

export interface MissionProjection {
  mission_id: string;
  status: MissionStatus;
  mission_status_hash: string;
  ledger_head: string;
  ledger_verified: boolean;
  evidence_verified: boolean;
  entry_count: number;
}

/** List mission ids that have a ledger to project, newest first. */
export async function listMissions(): Promise<string[]> {
  const body = await apiGet<{ missions: string[] }>('/missions');
  return body.missions ?? [];
}

/** Fetch the projection receipt (canonical status + status hash) for a mission. */
export function getMission(missionId: string): Promise<MissionProjection> {
  return apiGet<MissionProjection>(`/missions/${encodeURIComponent(missionId)}`);
}

/** Build the provenance link for a phase gate's evidence bundle. */
export function missionEvidenceUrl(missionId: string, taskId: string): string {
  return buildUrl(
    `/missions/${encodeURIComponent(missionId)}/evidence/${encodeURIComponent(taskId)}`,
  );
}
