/**
 * @bernstein/sdk — Integration SDK for the Bernstein task server.
 *
 * @example
 * ```ts
 * import { BernsteinClient } from '@bernstein/sdk';
 *
 * const client = new BernsteinClient({ baseUrl: 'http://127.0.0.1:8052' });
 * const task = await client.createTask({ title: 'Fix login bug', role: 'backend' });
 * await client.completeTask(task.id, 'Patched null-check in auth.py');
 * ```
 */

export { BernsteinClient } from './client.js';

export type {
  TaskCreate,
  TaskResponse,
  TaskStatus,
  TaskScope,
  TaskComplexity,
  StatusSummary,
  ClientOptions,
} from './models.js';

export {
  jiraToBernstein,
  bernsteinToJira,
  linearToBernstein,
  bernsteinToLinear,
  registerJiraMapping,
  registerBernsteinToJiraMapping,
  registerLinearMapping,
  registerBernsteinToLinearMapping,
} from './state-map.js';
