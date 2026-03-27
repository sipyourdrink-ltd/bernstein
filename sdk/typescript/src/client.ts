/**
 * HTTP client for the Bernstein task server.
 *
 * Uses the built-in `fetch` API (available in Node.js 18+).  No extra
 * dependencies required.
 *
 * @example
 * ```ts
 * import { BernsteinClient } from '@bernstein/sdk';
 *
 * const client = new BernsteinClient({ baseUrl: 'http://127.0.0.1:8052' });
 *
 * const task = await client.createTask({
 *   title: 'Fix login regression',
 *   role: 'backend',
 *   priority: 1,
 * });
 * console.log(task.id, task.status);
 *
 * await client.completeTask(task.id, 'Patched null-check in auth.py');
 * ```
 */

import type {
  ClientOptions,
  StatusSummary,
  TaskCreate,
  TaskResponse,
  TaskStatus,
} from './models.js';

const DEFAULT_BASE_URL = 'http://127.0.0.1:8052';
const DEFAULT_TIMEOUT_MS = 10_000;

export class BernsteinClient {
  private readonly baseUrl: string;
  private readonly headers: Record<string, string>;
  private readonly timeoutMs: number;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, '');
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.headers = { 'Content-Type': 'application/json' };
    if (options.token) {
      this.headers['Authorization'] = `Bearer ${options.token}`;
    }
  }

  // -------------------------------------------------------------------------
  // Task operations
  // -------------------------------------------------------------------------

  /**
   * Create a new task on the Bernstein task server.
   *
   * @throws {Error} If the server returns a non-2xx response.
   */
  async createTask(params: TaskCreate): Promise<TaskResponse> {
    const res = await this.post<TaskResponse>('/tasks', params);
    return res;
  }

  /** Fetch a single task by ID. */
  async getTask(taskId: string): Promise<TaskResponse> {
    return this.get<TaskResponse>(`/tasks/${taskId}`);
  }

  /**
   * List tasks, optionally filtered by status.
   *
   * @param status - If provided, only tasks in this state are returned.
   */
  async listTasks(status?: TaskStatus): Promise<TaskResponse[]> {
    const url = status ? `/tasks?status=${status}` : '/tasks';
    const data = await this.get<TaskResponse[] | { tasks: TaskResponse[] }>(url);
    return Array.isArray(data) ? data : data.tasks;
  }

  /**
   * Mark a task as `done`.
   *
   * @param taskId - Task to complete.
   * @param resultSummary - Brief description of what was accomplished.
   */
  async completeTask(taskId: string, resultSummary = ''): Promise<void> {
    await this.post(`/tasks/${taskId}/complete`, { result_summary: resultSummary });
  }

  /**
   * Mark a task as `failed`.
   *
   * @param taskId - Task to fail.
   * @param error - Error message or failure reason.
   */
  async failTask(taskId: string, error = ''): Promise<void> {
    await this.post(`/tasks/${taskId}/fail`, { error });
  }

  /** Return aggregate statistics from `GET /status`. */
  async getStatus(): Promise<StatusSummary> {
    return this.get<StatusSummary>('/status');
  }

  /** Return `true` if the server is reachable and healthy. */
  async health(): Promise<boolean> {
    try {
      const res = await fetchWithTimeout(
        `${this.baseUrl}/health`,
        { headers: this.headers },
        this.timeoutMs,
      );
      return res.ok;
    } catch {
      return false;
    }
  }

  // -------------------------------------------------------------------------
  // HTTP helpers
  // -------------------------------------------------------------------------

  private async get<T>(path: string): Promise<T> {
    const res = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      { headers: this.headers },
      this.timeoutMs,
    );
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(`Bernstein API error ${res.status}: ${body.slice(0, 300)}`);
    }
    return res.json() as Promise<T>;
  }

  private async post<T = void>(path: string, body: unknown): Promise<T> {
    const res = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      {
        method: 'POST',
        headers: this.headers,
        body: JSON.stringify(body),
      },
      this.timeoutMs,
    );
    if (!res.ok) {
      const errBody = await res.text().catch(() => '');
      throw new Error(`Bernstein API error ${res.status}: ${errBody.slice(0, 300)}`);
    }
    // Some endpoints return 204 No Content
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }
}

function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...init, signal: controller.signal }).finally(() =>
    clearTimeout(timer),
  );
}
