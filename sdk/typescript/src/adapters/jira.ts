/**
 * Jira Cloud adapter — parse webhook payloads and map states.
 *
 * This module is intentionally lightweight: it does not bundle the Jira
 * REST API client.  Use it to parse incoming webhook payloads and generate
 * {@link TaskCreate} objects for the Bernstein client.
 *
 * @example
 * ```ts
 * import { JiraAdapter } from '@bernstein/sdk/adapters/jira';
 * import { BernsteinClient } from '@bernstein/sdk';
 *
 * const adapter = new JiraAdapter({ defaultRole: 'backend' });
 * const taskCreate = adapter.taskFromWebhook(req.body);
 * if (taskCreate) {
 *   await client.createTask(taskCreate);
 * }
 * ```
 */

import type { TaskCreate, TaskStatus } from '../models.js';
import { jiraToBernstein, bernsteinToJira } from '../state-map.js';

export interface JiraIssueRef {
  key: string;           // e.g. "PROJ-42"
  summary: string;
  description: string;
  status: string;        // Jira status name
  priority: string;      // "Highest" | "High" | "Medium" | "Low" | "Lowest"
  storyPoints: number | null;
  labels: string[];
  assigneeEmail: string | null;
}

export interface JiraAdapterOptions {
  /** Default Bernstein agent role for created tasks. Default: "backend". */
  defaultRole?: string;
  /**
   * Map Jira project keys to Bernstein roles.
   * e.g. `{ "OPS": "infrastructure", "SEC": "security" }`.
   */
  projectKeyToRole?: Record<string, string>;
}

const JIRA_PRIORITY_MAP: Record<string, 1 | 2 | 3> = {
  highest: 1,
  high: 1,
  medium: 2,
  low: 3,
  lowest: 3,
};

export class JiraAdapter {
  private readonly defaultRole: string;
  private readonly projectKeyToRole: Record<string, string>;

  constructor(options: JiraAdapterOptions = {}) {
    this.defaultRole = options.defaultRole ?? 'backend';
    this.projectKeyToRole = options.projectKeyToRole ?? {};
  }

  /**
   * Parse a Jira `issue_created` or `issue_updated` webhook payload and
   * return a {@link TaskCreate}, or `null` if the issue is terminal / unusable.
   */
  taskFromWebhook(payload: Record<string, unknown>): TaskCreate | null {
    const issue = extractIssue(payload);
    if (!issue) return null;

    const mapped = jiraToBernstein(issue.status);
    if (mapped === 'done' || mapped === 'cancelled') return null;

    return this.taskFromIssue(issue);
  }

  /** Convert a parsed {@link JiraIssueRef} to a {@link TaskCreate}. */
  taskFromIssue(issue: JiraIssueRef): TaskCreate {
    const projectKey = issue.key.includes('-') ? issue.key.split('-')[0]! : '';
    const role = this.projectKeyToRole[projectKey] ?? this.defaultRole;
    const priorityKey = issue.priority.toLowerCase();
    const priority: 1 | 2 | 3 = JIRA_PRIORITY_MAP[priorityKey] ?? 2;
    const scope = storyPointsToScope(issue.storyPoints);
    const complexity = labelsToComplexity(issue.labels);

    return {
      title: `[${issue.key}] ${issue.summary}`,
      role,
      description: issue.description,
      priority,
      scope,
      complexity,
      external_ref: `jira:${issue.key}`,
      metadata: {
        jira_key: issue.key,
        jira_status: issue.status,
        jira_labels: issue.labels,
      },
    };
  }

  /**
   * Return the Jira status name to transition to for a given Bernstein state.
   *
   * Use this when pushing state changes back to Jira after a task completes.
   */
  jiraStatusFor(status: TaskStatus): string {
    return bernsteinToJira(status);
  }
}

// ---------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------

function extractIssue(payload: Record<string, unknown>): JiraIssueRef | null {
  const issue = payload['issue'];
  if (!issue || typeof issue !== 'object') return null;
  const issueObj = issue as Record<string, unknown>;
  const key = String(issueObj['key'] ?? '');
  const fields = (issueObj['fields'] ?? {}) as Record<string, unknown>;

  const statusObj = (fields['status'] ?? {}) as Record<string, unknown>;
  const priorityObj = (fields['priority'] ?? {}) as Record<string, unknown>;
  const assigneeObj = (fields['assignee'] ?? {}) as Record<string, unknown>;
  const descRaw = fields['description'];
  const description =
    typeof descRaw === 'string'
      ? descRaw
      : descRaw && typeof descRaw === 'object'
        ? extractAdfText(descRaw as Record<string, unknown>)
        : '';

  return {
    key,
    summary: String(fields['summary'] ?? ''),
    description,
    status: String(statusObj['name'] ?? 'open'),
    priority: String((priorityObj['name'] ?? 'medium')),
    storyPoints:
      typeof fields['story_points'] === 'number'
        ? fields['story_points']
        : typeof fields['customfield_10016'] === 'number'
          ? fields['customfield_10016']
          : null,
    labels: Array.isArray(fields['labels'])
      ? (fields['labels'] as unknown[]).map(String)
      : [],
    assigneeEmail: typeof assigneeObj['emailAddress'] === 'string'
      ? assigneeObj['emailAddress']
      : null,
  };
}

function extractAdfText(node: Record<string, unknown>, depth = 0): string {
  if (depth > 20) return '';
  const text = typeof node['text'] === 'string' ? node['text'] : '';
  const content = Array.isArray(node['content'])
    ? (node['content'] as Record<string, unknown>[]).map((c) => extractAdfText(c, depth + 1))
    : [];
  return [text, ...content].filter(Boolean).join(' ').trim();
}

function storyPointsToScope(
  points: number | null,
): 'small' | 'medium' | 'large' {
  if (points === null) return 'medium';
  if (points <= 3) return 'small';
  if (points <= 8) return 'medium';
  return 'large';
}

function labelsToComplexity(
  labels: string[],
): 'low' | 'medium' | 'high' {
  const lower = new Set(labels.map((l) => l.toLowerCase()));
  const highSignals = new Set(['complex', 'high-complexity', 'architecture', 'security']);
  const lowSignals = new Set(['simple', 'easy', 'docs', 'documentation']);
  if ([...lower].some((l) => highSignals.has(l))) return 'high';
  if ([...lower].some((l) => lowSignals.has(l))) return 'low';
  return 'medium';
}
