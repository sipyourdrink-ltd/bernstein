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
    this.item.text = '🎼 Bernstein';
    this.item.tooltip = 'Bernstein Orchestrator — click to open dashboard (connecting…)';
    this.item.show();
  }

  update(data: DashboardData): void {
    const { stats } = data;
    const total = stats.total;
    const cost = `$${stats.cost_usd.toFixed(2)}`;
    const agents = stats.agents;
    const tasks = `${stats.done}/${total}`;
    this.item.text = `🎼 ${agents} agents · ${tasks} tasks · ${cost}`;
    this.item.tooltip =
      `Bernstein — ${agents} active agents | ${tasks} tasks done | ${cost} total cost`;
  }

  setError(message: string): void {
    this.item.text = '🎼 not connected';
    this.item.tooltip = `Bernstein: ${message}`;
  }

  dispose(): void {
    this.item.dispose();
  }
}
