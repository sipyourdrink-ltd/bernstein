/**
 * Bidirectional state mappings between Bernstein task states and external trackers.
 *
 * @example
 * ```ts
 * import { jiraToBernstein, bernsteinToJira } from '@bernstein/sdk/state-map';
 *
 * const status = jiraToBernstein('In Progress'); // → 'in_progress'
 * const label  = bernsteinToJira('done');         // → 'Done'
 * ```
 */

import type { TaskStatus } from './models.js';

// ---------------------------------------------------------------------------
// Jira ↔ Bernstein
// ---------------------------------------------------------------------------

const JIRA_TO_BERNSTEIN: Record<string, TaskStatus> = {
  'backlog': 'open',
  'to do': 'open',
  'open': 'open',
  'new': 'open',
  'selected for development': 'open',
  'in progress': 'in_progress',
  'in review': 'in_progress',
  'under review': 'in_progress',
  'code review': 'in_progress',
  'blocked': 'blocked',
  'waiting': 'blocked',
  'on hold': 'blocked',
  'waiting for review': 'blocked',
  'done': 'done',
  'closed': 'done',
  'resolved': 'done',
  "won't do": 'cancelled',
  'wont do': 'cancelled',
  'cancelled': 'cancelled',
  'duplicate': 'cancelled',
};

const BERNSTEIN_TO_JIRA: Record<TaskStatus, string> = {
  open: 'To Do',
  claimed: 'In Progress',
  in_progress: 'In Progress',
  done: 'Done',
  failed: 'Done',
  blocked: 'Blocked',
  cancelled: "Won't Do",
  orphaned: 'To Do',
};

/** Project-specific overrides applied on top of the default mapping. */
const jiraOverrides: Record<string, TaskStatus> = {};
const bernsteinToJiraOverrides: Partial<Record<TaskStatus, string>> = {};

/**
 * Map a Jira issue status name (case-insensitive) to a {@link TaskStatus}.
 * Unknown statuses return `fallback` (default: `"open"`).
 */
export function jiraToBernstein(
  jiraStatus: string,
  fallback: TaskStatus = 'open',
): TaskStatus {
  const key = jiraStatus.toLowerCase().trim();
  return jiraOverrides[key] ?? JIRA_TO_BERNSTEIN[key] ?? fallback;
}

/**
 * Map a {@link TaskStatus} to a Jira status transition name.
 */
export function bernsteinToJira(status: TaskStatus): string {
  return bernsteinToJiraOverrides[status] ?? BERNSTEIN_TO_JIRA[status];
}

/**
 * Register a project-specific Jira status mapping that overrides the defaults.
 */
export function registerJiraMapping(
  jiraStatus: string,
  bernsteinStatus: TaskStatus,
): void {
  jiraOverrides[jiraStatus.toLowerCase().trim()] = bernsteinStatus;
}

/**
 * Override the default Jira target for a given Bernstein state.
 */
export function registerBernsteinToJiraMapping(
  bernsteinStatus: TaskStatus,
  jiraStatus: string,
): void {
  bernsteinToJiraOverrides[bernsteinStatus] = jiraStatus;
}

// ---------------------------------------------------------------------------
// Linear ↔ Bernstein
// ---------------------------------------------------------------------------

const LINEAR_TO_BERNSTEIN: Record<string, TaskStatus> = {
  triage: 'open',
  backlog: 'open',
  unstarted: 'open',
  todo: 'open',
  started: 'in_progress',
  'in progress': 'in_progress',
  'in review': 'in_progress',
  completed: 'done',
  done: 'done',
  cancelled: 'cancelled',
  canceled: 'cancelled',
  duplicate: 'cancelled',
  blocked: 'blocked',
  waiting: 'blocked',
};

const BERNSTEIN_TO_LINEAR: Record<TaskStatus, string> = {
  open: 'Todo',
  claimed: 'In Progress',
  in_progress: 'In Progress',
  done: 'Done',
  failed: 'Cancelled',
  blocked: 'Blocked',
  cancelled: 'Cancelled',
  orphaned: 'Todo',
};

const linearOverrides: Record<string, TaskStatus> = {};
const bernsteinToLinearOverrides: Partial<Record<TaskStatus, string>> = {};

/**
 * Map a Linear issue state name or type (case-insensitive) to a {@link TaskStatus}.
 */
export function linearToBernstein(
  linearState: string,
  fallback: TaskStatus = 'open',
): TaskStatus {
  const key = linearState.toLowerCase().trim();
  return linearOverrides[key] ?? LINEAR_TO_BERNSTEIN[key] ?? fallback;
}

/**
 * Map a {@link TaskStatus} to a Linear state name.
 */
export function bernsteinToLinear(status: TaskStatus): string {
  return bernsteinToLinearOverrides[status] ?? BERNSTEIN_TO_LINEAR[status];
}

/** Register a workspace-specific Linear state mapping. */
export function registerLinearMapping(
  linearState: string,
  bernsteinStatus: TaskStatus,
): void {
  linearOverrides[linearState.toLowerCase().trim()] = bernsteinStatus;
}

/** Override the default Linear target for a given Bernstein state. */
export function registerBernsteinToLinearMapping(
  bernsteinStatus: TaskStatus,
  linearState: string,
): void {
  bernsteinToLinearOverrides[bernsteinStatus] = linearState;
}
