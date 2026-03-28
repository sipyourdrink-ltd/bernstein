import * as vscode from 'vscode';
import type { BernsteinAgent } from './BernsteinClient';

const ACTIVE_STATUSES = new Set(['active', 'busy']);

export class AgentItem extends vscode.TreeItem {
  constructor(public readonly agent: BernsteinAgent) {
    const isActive = ACTIVE_STATUSES.has(agent.status);
    const icon = isActive ? '$(circle-filled)' : '$(circle-outline)';
    super(`${icon} ${agent.id.slice(0, 14)}`, vscode.TreeItemCollapsibleState.None);

    const model = agent.model ?? agent.role;
    const runtime =
      agent.runtime_s > 60
        ? `${Math.floor(agent.runtime_s / 60)}m`
        : `${agent.runtime_s}s`;
    const cost = `$${agent.cost_usd.toFixed(2)}`;

    this.description = `${model}  ${runtime}  ${cost}`;

    this.tooltip = [
      `Agent: ${agent.id}`,
      `Role: ${agent.role}`,
      `Status: ${agent.status}`,
      `Runtime: ${runtime}`,
      `Cost: ${cost}`,
      agent.current_task ? `Task: ${agent.current_task}` : null,
    ]
      .filter(Boolean)
      .join('\n');

    this.contextValue = isActive ? 'agent.active' : 'agent.idle';

    // Single-click → open output channel
    this.command = {
      command: 'bernstein.showAgentOutput',
      title: 'Show Agent Output',
      arguments: [this],
    };
  }
}

export class AgentTreeProvider implements vscode.TreeDataProvider<AgentItem> {
  private readonly _onDidChangeTreeData =
    new vscode.EventEmitter<AgentItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private agents: BernsteinAgent[] = [];

  update(agents: BernsteinAgent[]): void {
    this.agents = agents;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: AgentItem): vscode.TreeItem {
    return element;
  }

  getChildren(): AgentItem[] {
    return this.agents.map((a) => new AgentItem(a));
  }
}
