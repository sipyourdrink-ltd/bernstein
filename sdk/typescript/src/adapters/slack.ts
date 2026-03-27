/**
 * Slack adapter — send Bernstein task notifications via Incoming Webhooks or Web API.
 *
 * Uses the built-in `fetch` API (Node.js 18+). No extra dependencies.
 *
 * @example
 * ```ts
 * import { SlackAdapter } from '@bernstein/sdk/adapters/slack';
 *
 * const slack = new SlackAdapter({ webhookUrl: process.env.SLACK_WEBHOOK_URL });
 * await slack.notifyTaskCompleted('abc123', 'Fix login regression', 'backend', 'Patched null-check');
 * ```
 */

import type { TaskStatus } from '../models.js';

export interface SlackAdapterOptions {
  webhookUrl?: string;
  botToken?: string;
  channel?: string;
  /** Slack user/group to @mention on failures (e.g. "<!here>" or "<@U123>"). */
  mentionOnFailure?: string;
}

export interface SlackBlock {
  type: string;
  [key: string]: unknown;
}

export class SlackAdapter {
  private readonly webhookUrl: string;
  private readonly botToken: string;
  private readonly channel: string;
  private readonly mentionOnFailure: string;

  constructor(options: SlackAdapterOptions = {}) {
    this.webhookUrl = options.webhookUrl ?? '';
    this.botToken = options.botToken ?? '';
    this.channel = options.channel ?? '';
    this.mentionOnFailure = options.mentionOnFailure ?? '';

    if (!this.webhookUrl && !this.botToken) {
      console.warn('SlackAdapter: no webhook URL or bot token configured');
    }
  }

  static fromEnv(): SlackAdapter {
    return new SlackAdapter({
      webhookUrl: process.env['SLACK_WEBHOOK_URL'] ?? '',
      botToken: process.env['SLACK_BOT_TOKEN'] ?? '',
      channel: process.env['SLACK_CHANNEL'] ?? '',
      mentionOnFailure: process.env['SLACK_MENTION_ON_FAILURE'] ?? '',
    });
  }

  async notifyTaskCompleted(
    taskId: string,
    title: string,
    role: string,
    resultSummary = '',
    channel?: string,
  ): Promise<void> {
    const blocks = taskCompletedBlocks(taskId, title, role, resultSummary);
    await this.post(blocks, channel ?? this.channel);
  }

  async notifyTaskFailed(
    taskId: string,
    title: string,
    role: string,
    error = '',
    channel?: string,
  ): Promise<void> {
    const blocks = taskFailedBlocks(taskId, title, role, error, this.mentionOnFailure);
    await this.post(blocks, channel ?? this.channel);
  }

  async notifyTaskCreated(
    taskId: string,
    title: string,
    role: string,
    priority = 2,
    channel?: string,
  ): Promise<void> {
    const blocks = taskCreatedBlocks(taskId, title, role, priority);
    await this.post(blocks, channel ?? this.channel);
  }

  async postMessage(
    text: string,
    blocks?: SlackBlock[],
    channel?: string,
  ): Promise<void> {
    await this.post(blocks ?? [], channel ?? this.channel, text);
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private async post(
    blocks: SlackBlock[],
    channel: string,
    text?: string,
  ): Promise<void> {
    const fallbackText = text ?? blocksToText(blocks);
    try {
      if (this.webhookUrl) {
        await fetchJson(this.webhookUrl, { blocks, text: fallbackText });
      } else if (this.botToken && channel) {
        await fetchJson(
          'https://slack.com/api/chat.postMessage',
          { channel, blocks, text: fallbackText },
          { Authorization: `Bearer ${this.botToken}` },
        );
      }
    } catch (err) {
      console.warn('SlackAdapter: failed to post message:', err);
    }
  }
}

// ---------------------------------------------------------------------------
// Block Kit builders
// ---------------------------------------------------------------------------

function taskCompletedBlocks(
  taskId: string,
  title: string,
  role: string,
  summary: string,
): SlackBlock[] {
  const fields: Record<string, unknown>[] = [
    { type: 'mrkdwn', text: `*Title:*\n${title}` },
    { type: 'mrkdwn', text: `*Role:*\n\`${role}\`` },
  ];
  if (summary) {
    fields.push({ type: 'mrkdwn', text: `*Result:*\n${summary.slice(0, 300)}` });
  }
  return [
    { type: 'section', text: { type: 'mrkdwn', text: `:white_check_mark: *Task completed* — \`${taskId}\`` } },
    { type: 'section', fields },
  ];
}

function taskFailedBlocks(
  taskId: string,
  title: string,
  role: string,
  error: string,
  mention: string,
): SlackBlock[] {
  let header = `:x: *Task failed* — \`${taskId}\``;
  if (mention) header = `${mention} ${header}`;
  const blocks: SlackBlock[] = [
    { type: 'section', text: { type: 'mrkdwn', text: header } },
    {
      type: 'section',
      fields: [
        { type: 'mrkdwn', text: `*Title:*\n${title}` },
        { type: 'mrkdwn', text: `*Role:*\n\`${role}\`` },
      ],
    },
  ];
  if (error) {
    blocks.push({
      type: 'section',
      text: { type: 'mrkdwn', text: `*Error:*\n\`\`\`${error.slice(0, 500)}\`\`\`` },
    });
  }
  return blocks;
}

function taskCreatedBlocks(
  taskId: string,
  title: string,
  role: string,
  priority: number,
): SlackBlock[] {
  const emoji =
    priority === 1 ? ':rotating_light:' : priority === 3 ? ':white_circle:' : ':blue_circle:';
  return [
    {
      type: 'section',
      text: { type: 'mrkdwn', text: `${emoji} *New task* — \`${taskId}\`` },
      fields: [
        { type: 'mrkdwn', text: `*Title:*\n${title}` },
        { type: 'mrkdwn', text: `*Role:*\n\`${role}\`` },
      ],
    },
  ];
}

function blocksToText(blocks: SlackBlock[]): string {
  for (const block of blocks) {
    const textObj = block['text'];
    if (textObj && typeof textObj === 'object') {
      const t = (textObj as Record<string, unknown>)['text'];
      if (typeof t === 'string' && t) return t;
    }
  }
  return 'Bernstein notification';
}

async function fetchJson(
  url: string,
  body: unknown,
  extraHeaders: Record<string, string> = {},
): Promise<void> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
}

// Re-export TaskStatus so callers can build notify calls from task data
export type { TaskStatus };
