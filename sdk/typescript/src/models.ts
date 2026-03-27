/**
 * Typed models for the Bernstein SDK.
 */

export type TaskStatus =
  | 'open'
  | 'claimed'
  | 'in_progress'
  | 'done'
  | 'failed'
  | 'blocked'
  | 'cancelled'
  | 'orphaned';

export type TaskScope = 'small' | 'medium' | 'large';
export type TaskComplexity = 'low' | 'medium' | 'high';

/** Parameters for creating a new task. */
export interface TaskCreate {
  /** Short, imperative title (e.g. "Fix login regression"). */
  title: string;
  /** Agent role to assign (e.g. "backend", "qa", "security"). */
  role?: string;
  /** Full task brief shown to the agent. */
  description?: string;
  /** 1 = critical, 2 = normal, 3 = nice-to-have. */
  priority?: 1 | 2 | 3;
  /** Rough size estimate. */
  scope?: TaskScope;
  /** Reasoning complexity hint for model selection. */
  complexity?: TaskComplexity;
  /** Expected wall-clock minutes. */
  estimated_minutes?: number;
  /** Task IDs that must complete first. */
  depends_on?: string[];
  /** Opaque back-reference to the source issue (e.g. "jira:PROJ-42"). */
  external_ref?: string;
  /** Arbitrary key-value pairs attached to the task. */
  metadata?: Record<string, unknown>;
}

/** A task as returned by the Bernstein task server. */
export interface TaskResponse {
  id: string;
  title: string;
  role: string;
  status: TaskStatus;
  priority: number;
  scope: TaskScope;
  complexity: TaskComplexity;
  description: string;
  assigned_agent?: string;
  result_summary?: string;
  external_ref: string;
  metadata: Record<string, unknown>;
  created_at: number;
}

/** Aggregate statistics from GET /status. */
export interface StatusSummary {
  total: number;
  open: number;
  claimed: number;
  done: number;
  failed: number;
  agents: number;
  cost_usd: number;
}

/** Options for {@link BernsteinClient}. */
export interface ClientOptions {
  /** Bernstein task server base URL. Default: "http://127.0.0.1:8052". */
  baseUrl?: string;
  /** Bearer token for authenticated servers. */
  token?: string;
  /** Request timeout in milliseconds. Default: 10000. */
  timeoutMs?: number;
}
