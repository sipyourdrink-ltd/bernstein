/**
 * Microsoft Teams adapter — send Bernstein task events via Incoming Webhooks.
 *
 * Uses the built-in `fetch` API (Node.js 18+). No extra dependencies.
 *
 * @example
 * ```ts
 * import { TeamsAdapter } from '@bernstein/sdk/adapters/teams';
 *
 * const teams = new TeamsAdapter({ webhookUrl: process.env.TEAMS_WEBHOOK_URL });
 * await teams.notifyTaskCompleted('abc123', 'Add rate limiting', 'backend', 'Implemented token bucket');
 * ```
 */

export interface TeamsAdapterOptions {
  webhookUrl?: string;
}

export class TeamsAdapter {
  private readonly webhookUrl: string;

  constructor(options: TeamsAdapterOptions = {}) {
    this.webhookUrl = options.webhookUrl ?? '';
    if (!this.webhookUrl) {
      console.warn(
        'TeamsAdapter: no webhook URL configured — set TEAMS_WEBHOOK_URL or pass webhookUrl=',
      );
    }
  }

  static fromEnv(): TeamsAdapter {
    return new TeamsAdapter({ webhookUrl: process.env['TEAMS_WEBHOOK_URL'] ?? '' });
  }

  async notifyTaskCompleted(
    taskId: string,
    title: string,
    role: string,
    resultSummary = '',
  ): Promise<void> {
    await this.post(
      completedCard(taskId, title, role, resultSummary),
    );
  }

  async notifyTaskFailed(
    taskId: string,
    title: string,
    role: string,
    error = '',
  ): Promise<void> {
    await this.post(failedCard(taskId, title, role, error));
  }

  async notifyTaskCreated(
    taskId: string,
    title: string,
    role: string,
    priority = 2,
  ): Promise<void> {
    await this.post(createdCard(taskId, title, role, priority));
  }

  async postMessage(text: string): Promise<void> {
    await this.post(textCard(text));
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private async post(payload: unknown): Promise<void> {
    if (!this.webhookUrl) return;
    try {
      const res = await fetch(this.webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const body = await res.text().catch(() => '');
        console.warn(`TeamsAdapter: webhook returned ${res.status}: ${body.slice(0, 200)}`);
      }
    } catch (err) {
      console.warn('TeamsAdapter: failed to post webhook:', err);
    }
  }
}

// ---------------------------------------------------------------------------
// Adaptive Card builders
// ---------------------------------------------------------------------------

interface AdaptiveCardFact {
  title: string;
  value: string;
}

function adaptiveCard(
  title: string,
  subtitle: string,
  color: string,
  facts: AdaptiveCardFact[],
): unknown {
  return {
    type: 'message',
    attachments: [
      {
        contentType: 'application/vnd.microsoft.card.adaptive',
        contentUrl: null,
        content: {
          $schema: 'http://adaptivecards.io/schemas/adaptive-card.json',
          type: 'AdaptiveCard',
          version: '1.4',
          body: [
            { type: 'TextBlock', size: 'Large', weight: 'Bolder', text: title, color },
            { type: 'TextBlock', text: subtitle, wrap: true },
            { type: 'FactSet', facts },
          ],
        },
      },
    ],
  };
}

function completedCard(
  taskId: string,
  title: string,
  role: string,
  summary: string,
): unknown {
  const facts: AdaptiveCardFact[] = [
    { title: 'Task ID', value: taskId },
    { title: 'Role', value: role },
  ];
  if (summary) facts.push({ title: 'Result', value: summary.slice(0, 400) });
  return adaptiveCard('✅ Task Completed', title, 'Good', facts);
}

function failedCard(
  taskId: string,
  title: string,
  role: string,
  error: string,
): unknown {
  const facts: AdaptiveCardFact[] = [
    { title: 'Task ID', value: taskId },
    { title: 'Role', value: role },
  ];
  if (error) facts.push({ title: 'Error', value: error.slice(0, 400) });
  return adaptiveCard('❌ Task Failed', title, 'Attention', facts);
}

function createdCard(
  taskId: string,
  title: string,
  role: string,
  priority: number,
): unknown {
  const priorityLabel =
    priority === 1 ? 'Critical' : priority === 3 ? 'Low' : 'Normal';
  return adaptiveCard('🆕 New Task', title, 'Accent', [
    { title: 'Task ID', value: taskId },
    { title: 'Role', value: role },
    { title: 'Priority', value: priorityLabel },
  ]);
}

function textCard(text: string): unknown {
  return {
    type: 'message',
    attachments: [
      {
        contentType: 'application/vnd.microsoft.card.adaptive',
        contentUrl: null,
        content: {
          $schema: 'http://adaptivecards.io/schemas/adaptive-card.json',
          type: 'AdaptiveCard',
          version: '1.4',
          body: [{ type: 'TextBlock', text, wrap: true }],
        },
      },
    ],
  };
}
