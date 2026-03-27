import * as vscode from 'vscode';
import type { DashboardData, BernsteinAgent } from './BernsteinClient';

function getNonce(): string {
  const chars =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from(
    { length: 32 },
    () => chars[Math.floor(Math.random() * chars.length)],
  ).join('');
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Build a 100×24 SVG sparkline from a rolling cost history array. */
function buildSparkline(values: number[]): string {
  if (values.length < 2) return '';
  const W = 100, H = 24;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 0.001;
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - ((v - min) / range) * (H - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" `
    + `class="sparkline" aria-hidden="true">`
    + `<polyline points="${points}" fill="none" stroke="currentColor" `
    + `stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`
    + `</svg>`;
}

/** Build agent cards for active agents only. */
function buildAgentCards(agents: BernsteinAgent[]): string {
  const active = agents.filter(
    (a) => a.status === 'working' || a.status === 'starting',
  );
  if (!active.length) return '';

  const cards = active.map((agent) => {
    const runtime =
      agent.runtime_s > 60
        ? `${Math.floor(agent.runtime_s / 60)}m`
        : `${agent.runtime_s}s`;
    const cost = `$${agent.cost_usd.toFixed(2)}`;
    const model = escapeHtml(agent.model ?? agent.role);
    const tasks = agent.tasks ?? [];
    const currentTask =
      tasks.find((t) => t.status === 'in_progress') ?? tasks[0];
    const progress = Math.min(100, Math.max(0, currentTask?.progress ?? 0));
    const agentId = escapeHtml(agent.id.slice(0, 14));

    return `<div class="agent-card">
  <div class="agent-row">
    <span class="dot-active">●</span>
    <span class="agent-id">${agentId}</span>
    <span class="agent-meta">${model} · ${runtime} · ${cost}</span>
  </div>${
    currentTask
      ? `
  <div class="task-name">${escapeHtml(currentTask.title.slice(0, 52))}</div>
  <div class="progress-track"><div class="progress-fill" style="width:${progress}%"></div></div>`
      : ''
  }
</div>`;
  }).join('');

  return `<h2>Agents</h2>${cards}`;
}

/** 4 skeleton placeholder cards shown while data is loading. */
function buildSkeletonGrid(): string {
  return `<div class="stats-grid">
    <div class="skeleton stat-card"></div>
    <div class="skeleton stat-card"></div>
    <div class="skeleton stat-card"></div>
    <div class="skeleton stat-card"></div>
  </div>`;
}

function buildHtml(data: DashboardData | null, costHistory: number[]): string {
  const nonce = getNonce();
  const csp = `default-src 'none'; style-src 'nonce-${nonce}';`;

  const stats = data?.stats;
  const total = stats ? stats.total : 0;
  const successRate = total > 0 ? Math.round((stats!.done / total) * 100) : 0;
  const sparkline = buildSparkline(costHistory);

  const statsHtml = stats
    ? `
      <div class="stat-card">
        <div class="stat-value">${stats.agents}</div>
        <div class="stat-label">Agents</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${stats.done}/${total}</div>
        <div class="stat-label">Tasks</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${successRate}%</div>
        <div class="stat-label">Success</div>
      </div>
      <div class="stat-card cost-card">
        <div class="stat-value">$${stats.cost_usd.toFixed(2)}</div>
        <div class="stat-label">Cost${sparkline ? ` <span class="sparkline-wrap">${sparkline}</span>` : ''}</div>
      </div>`
    : null;

  const agentCardsHtml = data ? buildAgentCards(data.agents) : '';

  const alertsHtml = (data?.alerts ?? [])
    .map(
      (a) =>
        `<div class="alert alert-${escapeHtml(a.level)}">${escapeHtml(a.message)}</div>`,
    )
    .join('');

  const bodyContent = !data
    ? `${buildSkeletonGrid()}<div class="offline">Connecting to Bernstein…</div>`
    : `<h2>Overview</h2>
<div class="stats-grid">${statsHtml}</div>
${agentCardsHtml}
${alertsHtml ? `<h2>Alerts</h2><div class="alerts">${alertsHtml}</div>` : ''}`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Bernstein</title>
  <style nonce="${nonce}">
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 13px;
      line-height: 1.4;
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      padding: 12px;
    }
    h2 {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      color: var(--vscode-descriptionForeground);
      margin: 14px 0 6px;
    }
    h2:first-child { margin-top: 0; }
    .stats-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .stat-card {
      background: var(--vscode-editor-background);
      border-radius: 5px;
      padding: 10px 10px 8px;
    }
    .stat-value {
      font-size: 18px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      margin-bottom: 3px;
    }
    .stat-label {
      font-size: 10px;
      color: var(--vscode-descriptionForeground);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .sparkline-wrap {
      display: flex;
      align-items: center;
      margin-left: auto;
    }
    .sparkline {
      color: var(--vscode-charts-blue, #5794f2);
      display: block;
    }
    .agent-card {
      background: var(--vscode-editor-background);
      border-radius: 5px;
      padding: 8px 10px;
      margin-bottom: 4px;
    }
    .agent-row {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 3px;
    }
    .dot-active {
      font-size: 8px;
      color: var(--vscode-charts-green, #73c48f);
      flex-shrink: 0;
    }
    .agent-id {
      font-weight: 500;
      font-variant-numeric: tabular-nums;
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .agent-meta {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .task-name {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      margin-bottom: 5px;
      padding-left: 14px;
    }
    .progress-track {
      height: 2px;
      background: var(--vscode-editorWidget-border, rgba(128,128,128,0.15));
      border-radius: 1px;
      overflow: hidden;
    }
    .progress-fill {
      height: 100%;
      background: var(--vscode-charts-blue, #5794f2);
      border-radius: 1px;
      min-width: 2%;
    }
    .alerts { display: flex; flex-direction: column; gap: 6px; }
    .alert {
      padding: 8px 10px;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.4;
    }
    .alert-warning {
      background: var(--vscode-inputValidation-warningBackground);
      border: 1px solid var(--vscode-inputValidation-warningBorder);
    }
    .alert-error {
      background: var(--vscode-inputValidation-errorBackground);
      border: 1px solid var(--vscode-inputValidation-errorBorder);
    }
    .alert-info {
      background: var(--vscode-editor-background);
      opacity: 0.8;
    }
    .offline {
      color: var(--vscode-disabledForeground);
      font-size: 11px;
      padding: 8px 0 0;
      letter-spacing: 0.2px;
    }
    @keyframes shimmer {
      0%, 100% { opacity: 0.35; }
      50% { opacity: 0.65; }
    }
    .skeleton {
      background: var(--vscode-editor-background);
      border-radius: 5px;
      animation: shimmer 1.5s ease-in-out infinite;
      height: 52px;
    }
  </style>
</head>
<body>
  ${bodyContent}
</body>
</html>`;
}

export class DashboardProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private data: DashboardData | null = null;
  private readonly costHistory: number[] = [];

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: false };
    webviewView.webview.html = buildHtml(this.data, this.costHistory);
  }

  update(data: DashboardData): void {
    this.data = data;
    this.costHistory.push(data.stats.cost_usd);
    if (this.costHistory.length > 20) this.costHistory.shift();
    if (this.view) {
      this.view.webview.html = buildHtml(data, this.costHistory);
    }
  }

  /**
   * Opens the full Bernstein dashboard in the default browser.
   * VS Code webviews cannot iframe localhost due to CSP restrictions,
   * so we open externally.
   */
  static openInBrowser(baseUrl: string): void {
    void vscode.env.openExternal(vscode.Uri.parse(`${baseUrl}/dashboard`));
  }
}
