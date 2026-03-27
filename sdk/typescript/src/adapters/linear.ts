/**
 * Linear adapter — parse webhook payloads and map states.
 *
 * @example
 * ```ts
 * import { LinearAdapter } from '@bernstein/sdk/adapters/linear';
 *
 * const adapter = new LinearAdapter({ defaultRole: 'backend' });
 * const taskCreate = adapter.taskFromWebhook(req.body);
 * if (taskCreate) {
 *   await client.createTask(taskCreate);
 * }
 * ```
 */

import type { TaskCreate, TaskStatus } from '../models.js';
import { linearToBernstein, bernsteinToLinear } from '../state-map.js';

export interface LinearIssueRef {
  identifier: string;   // e.g. "ENG-42"
  title: string;
  description: string;
  stateName: string;    // e.g. "In Progress"
  stateType: string;    // e.g. "started"
  priority: number;     // 0–4
  estimate: number | null;
  labels: string[];
  teamId: string;
  assigneeEmail: string | null;
}

export interface LinearAdapterOptions {
  defaultRole?: string;
  /** Map Linear team IDs to Bernstein roles. */
  teamIdToRole?: Record<string, string>;
}

const LINEAR_PRIORITY_MAP: Record<number, 1 | 2 | 3> = {
  0: 2, // No priority
  1: 1, // Urgent
  2: 1, // High
  3: 2, // Normal
  4: 3, // Low
};

export class LinearAdapter {
  private readonly defaultRole: string;
  private readonly teamIdToRole: Record<string, string>;

  constructor(options: LinearAdapterOptions = {}) {
    this.defaultRole = options.defaultRole ?? 'backend';
    this.teamIdToRole = options.teamIdToRole ?? {};
  }

  /**
   * Parse a Linear `Issue` webhook payload and return a {@link TaskCreate},
   * or `null` if the issue is terminal or action is "remove".
   */
  taskFromWebhook(payload: Record<string, unknown>): TaskCreate | null {
    if (payload['action'] === 'remove') return null;
    if (payload['type'] !== 'Issue') return null;

    const issue = parseLinearIssue(payload['data'] as Record<string, unknown> | undefined);
    if (!issue) return null;

    const mapped =
      linearToBernstein(issue.stateType) !== 'open'
        ? linearToBernstein(issue.stateType)
        : linearToBernstein(issue.stateName);
    if (mapped === 'done' || mapped === 'cancelled') return null;

    return this.taskFromIssue(issue);
  }

  /** Convert a parsed {@link LinearIssueRef} to a {@link TaskCreate}. */
  taskFromIssue(issue: LinearIssueRef): TaskCreate {
    const role = this.teamIdToRole[issue.teamId] ?? this.defaultRole;
    const priority: 1 | 2 | 3 = LINEAR_PRIORITY_MAP[issue.priority] ?? 2;
    const scope = estimateToScope(issue.estimate);
    const complexity = labelsToComplexity(issue.labels);

    return {
      title: `[${issue.identifier}] ${issue.title}`,
      role,
      description: issue.description,
      priority,
      scope,
      complexity,
      external_ref: `linear:${issue.identifier}`,
      metadata: {
        linear_identifier: issue.identifier,
        linear_state: issue.stateName,
        linear_state_type: issue.stateType,
        linear_labels: issue.labels,
      },
    };
  }

  /** Return the Linear state name for a given Bernstein task status. */
  linearStateFor(status: TaskStatus): string {
    return bernsteinToLinear(status);
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function parseLinearIssue(
  data: Record<string, unknown> | undefined,
): LinearIssueRef | null {
  if (!data) return null;
  const state = (data['state'] ?? {}) as Record<string, unknown>;
  const assignee = (data['assignee'] ?? {}) as Record<string, unknown>;
  const team = (data['team'] ?? {}) as Record<string, unknown>;
  const labelsConn = (data['labels'] ?? {}) as Record<string, unknown>;
  const labelNodes = Array.isArray(labelsConn['nodes'])
    ? (labelsConn['nodes'] as Record<string, unknown>[])
    : [];

  return {
    identifier: String(data['identifier'] ?? ''),
    title: String(data['title'] ?? ''),
    description: String(data['description'] ?? ''),
    stateName: String(state['name'] ?? 'Todo'),
    stateType: String(state['type'] ?? 'unstarted'),
    priority: typeof data['priority'] === 'number' ? data['priority'] : 0,
    estimate:
      typeof data['estimate'] === 'number' ? data['estimate'] : null,
    labels: labelNodes.map((n) => String(n['name'] ?? '')),
    teamId: String(team['id'] ?? ''),
    assigneeEmail:
      typeof assignee['email'] === 'string' ? assignee['email'] : null,
  };
}

function estimateToScope(estimate: number | null): 'small' | 'medium' | 'large' {
  if (estimate === null) return 'medium';
  if (estimate <= 2) return 'small';
  if (estimate <= 5) return 'medium';
  return 'large';
}

function labelsToComplexity(labels: string[]): 'low' | 'medium' | 'high' {
  const lower = new Set(labels.map((l) => l.toLowerCase()));
  const high = new Set(['complex', 'high-complexity', 'architecture', 'security', 'performance']);
  const low = new Set(['simple', 'easy', 'docs', 'documentation', 'chore']);
  if ([...lower].some((l) => high.has(l))) return 'high';
  if ([...lower].some((l) => low.has(l))) return 'low';
  return 'medium';
}
