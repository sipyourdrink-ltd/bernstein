import * as vscode from 'vscode';
import type { DashboardData } from './BernsteinClient';

function getNonce(): string {
  const chars =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from(
    { length: 32 },
    () => chars[Math.floor(Math.random() * chars.length)],
  ).join('');
}

function buildHtml(data: DashboardData | null): string {
  const nonce = getNonce();
  const csp =
    `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';`;

  const stats = data?.stats;
  const total = stats ? stats.done + stats.open + stats.claimed + stats.failed : 0;
  const successRate = total > 0 ? Math.round((stats!.done / total) * 100) : 0;

  const statsHtml = stats
    ? `
      <div class="stat-card">
        <div class="stat-value">${stats.agent_count}</div>
        <div class="stat-label">Active Agents</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${stats.done}/${total}</div>
        <div class="stat-label">Tasks Complete</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${successRate}%</div>
        <div class="stat-label">Success Rate</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">$${stats.total_cost_usd.toFixed(2)}</div>
        <div class="stat-label">Total Cost</div>
      </div>`
    : '<div class="offline">Not connected to Bernstein</div>';

  const alertsHtml =
    (data?.alerts ?? [])
      .map((a) => `<div class="alert alert-${a.level}">${a.message}</div>`)
      .join('') || '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Bernstein Dashboard</title>
  <style nonce="${nonce}">
    * {
      box-sizing: border-box;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      margin: 0;
      padding: 16px 12px;
    }
    h2 {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--vscode-descriptionForeground);
      margin: 16px 0 8px 0;
    }
    h2:first-child {
      margin-top: 0;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 16px;
    }
    .stat-card {
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-editor-lineHighlightBorder);
      border-radius: 6px;
      padding: 12px;
      text-align: center;
    }
    .stat-value {
      font-size: 20px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      margin-bottom: 4px;
    }
    .stat-label {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      opacity: 0.8;
    }
    .alerts {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .alert {
      padding: 10px 12px;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.4;
    }
    .alert-warning {
      background: var(--vscode-inputValidation-warningBackground);
      border: 1px solid var(--vscode-inputValidation-warningBorder);
      color: var(--vscode-inputValidation-warningForeground);
    }
    .alert-error {
      background: var(--vscode-inputValidation-errorBackground);
      border: 1px solid var(--vscode-inputValidation-errorBorder);
      color: var(--vscode-inputValidation-errorForeground);
    }
    .alert-info {
      background: var(--vscode-inputValidation-infoBorder);
      border: 1px solid var(--vscode-inputValidation-infoBorder);
      color: var(--vscode-foreground);
      opacity: 0.8;
    }
    .offline {
      color: var(--vscode-disabledForeground);
      font-size: 13px;
      padding: 12px;
      text-align: center;
    }
  </style>
</head>
<body>
  <h2>Overview</h2>
  <div class="stats-grid">${statsHtml}</div>
  ${alertsHtml ? `<h2>Alerts</h2><div class="alerts">${alertsHtml}</div>` : ''}
</body>
</html>`;
}

export class DashboardProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private data: DashboardData | null = null;

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: false };
    webviewView.webview.html = buildHtml(this.data);
  }

  update(data: DashboardData): void {
    this.data = data;
    if (this.view) {
      this.view.webview.html = buildHtml(data);
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
