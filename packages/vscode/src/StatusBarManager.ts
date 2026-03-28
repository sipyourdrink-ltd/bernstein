import * as vscode from 'vscode';
import type { DashboardData } from './BernsteinClient';

export class StatusBarManager {
  private readonly item: vscode.StatusBarItem;

  constructor() {
    this.item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Left,
      100,
    );
    this.item.command = 'bernstein.showDashboard';
    this.item.text = '◌ Bernstein';
    this.item.tooltip = 'Bernstein Orchestrator — click to open dashboard (connecting…)';
    this.item.show();
  }

  update(data: DashboardData): void {
    const { stats } = data;
    const total = stats.done + stats.open + stats.claimed + stats.failed;
    const cost = `$${stats.total_cost_usd.toFixed(2)}`;
    const agents = stats.agent_count;
    const tasks = `${stats.done}/${total}`;
    const status = stats.claimed > 0 ? '●' : '○';
    this.item.text = `${status} ${agents} agents · ${tasks} tasks · ${cost}`;
    this.item.tooltip =
      `Bernstein — ${agents} active agents | ${tasks} tasks done | ${cost} total cost`;
  }

  setError(message: string): void {
    this.item.text = '○ Bernstein';
    this.item.tooltip = `Bernstein: ${message}`;
  }

  dispose(): void {
    this.item.dispose();
  }
}
