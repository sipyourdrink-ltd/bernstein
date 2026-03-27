import * as http from 'http';
import * as https from 'https';

export interface BernsteinStatus {
  total: number;
  open: number;
  claimed: number;
  done: number;
  failed: number;
  agents: number;
  cost_usd: number;
}

export interface BernsteinTask {
  id: string;
  title: string;
  role: string;
  status: string;
  priority: number;
  assigned_agent?: string;
  owned_files?: string[];
  created_at?: number;
  progress?: number;
}

export interface BernsteinAgent {
  id: string;
  role: string;
  status: string;
  model?: string;
  spawn_ts?: number;
  runtime_s: number;
  pid?: number;
  task_ids?: string[];
  agent_source?: string;
  parent_agent_id?: string;
  cost_usd: number;
  tasks?: Array<{ id: string; title: string; status: string; progress: number }>;
}

export interface DashboardData {
  ts: number;
  stats: {
    total: number;
    open: number;
    claimed: number;
    done: number;
    failed: number;
    agents: number;
    cost_usd: number;
  };
  tasks: BernsteinTask[];
  agents: BernsteinAgent[];
  live_costs: {
    spent_usd: number;
    budget_usd: number;
    percentage_used: number;
    should_warn: boolean;
    should_stop: boolean;
    per_agent?: Record<string, number>;
    per_model?: Record<string, number>;
  };
  alerts: Array<{ level: string; message: string; detail?: string }>;
}

export type SseCallback = (event: string, data: string) => void;

export class BernsteinClient {
  readonly baseUrl: string;
  private readonly token: string;

  constructor(baseUrl: string, token: string = '') {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.token = token;
  }

  headers(): Record<string, string> {
    const h: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.token) {
      h['Authorization'] = `Bearer ${this.token}`;
    }
    return h;
  }

  get<T>(path: string): Promise<T> {
    return new Promise((resolve, reject) => {
      const url = new URL(this.baseUrl + path);
      const lib = url.protocol === 'https:' ? https : http;
      const req = lib.get(url.toString(), { headers: this.headers() }, (res) => {
        let body = '';
        res.on('data', (chunk: Buffer) => { body += chunk.toString(); });
        res.on('end', () => {
          if (res.statusCode !== undefined && res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}: ${body}`));
            return;
          }
          try {
            resolve(JSON.parse(body) as T);
          } catch {
            reject(new Error(`Failed to parse JSON: ${body.slice(0, 200)}`));
          }
        });
      });
      req.on('error', reject);
      req.setTimeout(5000, () => { req.destroy(new Error('Request timeout')); });
    });
  }

  post(path: string, body: unknown = {}): Promise<void> {
    return new Promise((resolve, reject) => {
      const url = new URL(this.baseUrl + path);
      const lib = url.protocol === 'https:' ? https : http;
      const payload = JSON.stringify(body);
      const options = {
        method: 'POST',
        headers: { ...this.headers(), 'Content-Length': Buffer.byteLength(payload) },
      };
      const req = lib.request(url.toString(), options, (res) => {
        res.on('data', () => {});
        res.on('end', () => {
          if (res.statusCode !== undefined && res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}`));
            return;
          }
          resolve();
        });
      });
      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }

  getStatus(): Promise<BernsteinStatus> {
    return this.get<BernsteinStatus>('/status');
  }

  getDashboardData(): Promise<DashboardData> {
    return this.get<DashboardData>('/dashboard/data');
  }

  killAgent(sessionId: string): Promise<void> {
    return this.post(`/agents/${sessionId}/kill`);
  }

  cancelTask(taskId: string): Promise<void> {
    return this.post(`/tasks/${taskId}/cancel`, { reason: 'Cancelled by user' });
  }

  prioritizeTask(taskId: string): Promise<void> {
    return this.post(`/tasks/${taskId}/prioritize`);
  }

  /**
   * Subscribe to the SSE /events stream.
   * Returns a stop function — call it to disconnect.
   * Automatically reconnects on error/close.
   */
  subscribeToEvents(onEvent: SseCallback, onError?: (err: Error) => void): () => void {
    const url = new URL(this.baseUrl + '/events');
    const lib = url.protocol === 'https:' ? https : http;
    let aborted = false;

    const connect = (): void => {
      if (aborted) return;
      const req = lib.get(
        url.toString(),
        { headers: { ...this.headers(), Accept: 'text/event-stream' } },
        (res) => {
          let buffer = '';
          let eventName = 'message';

          res.on('data', (chunk: Buffer) => {
            buffer += chunk.toString();
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';
            for (const line of lines) {
              if (line.startsWith('event: ')) {
                eventName = line.slice(7).trim();
              } else if (line.startsWith('data: ')) {
                onEvent(eventName, line.slice(6).trim());
                eventName = 'message';
              }
            }
          });

          res.on('end', () => {
            if (!aborted) setTimeout(connect, 3000);
          });
        },
      );

      req.on('error', (err: Error) => {
        if (!aborted) {
          onError?.(err);
          setTimeout(connect, 5000);
        }
      });
    };

    connect();
    return () => { aborted = true; };
  }
}
