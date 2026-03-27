import * as vscode from 'vscode';
import type { BernsteinAgent } from './BernsteinClient';

const ACTIVE_STATUSES = new Set(['working', 'starting']);

export class AgentItem extends vscode.TreeItem {
  readonly children: AgentItem[] = [];

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

    const taskInfo = agent.tasks?.length
      ? agent.tasks.map((t) => `${t.title} (${t.status})`).join(', ')
      : null;

    this.tooltip = [
      `Agent: ${agent.id}`,
      `Role: ${agent.role}`,
      `Status: ${agent.status}`,
      `Runtime: ${runtime}`,
      `Cost: ${cost}`,
      taskInfo ? `Tasks: ${taskInfo}` : null,
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

  /** Update collapsible state once children are known. */
  updateCollapsibleState(): void {
    this.collapsibleState = this.children.length > 0
      ? vscode.TreeItemCollapsibleState.Expanded
      : vscode.TreeItemCollapsibleState.None;
  }
}

export class AgentTreeProvider implements vscode.TreeDataProvider<AgentItem> {
  private readonly _onDidChangeTreeData =
    new vscode.EventEmitter<AgentItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private roots: AgentItem[] = [];

  update(agents: BernsteinAgent[]): void {
    this.roots = buildDelegationTree(agents);
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: AgentItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: AgentItem): AgentItem[] {
    return element ? element.children : this.roots;
  }
}

/**
 * Build a delegation tree from a flat list of agents.
 *
 * Uses the `parent_agent_id` field when present so that cell workers appear
 * nested under their manager in the VS Code sidebar tree.  Agents without a
 * parent are displayed as roots (same as the previous flat list).
 */
function buildDelegationTree(agents: BernsteinAgent[]): AgentItem[] {
  const idToItem = new Map<string, AgentItem>();
  for (const agent of agents) {
    idToItem.set(agent.id, new AgentItem(agent));
  }

  const roots: AgentItem[] = [];

  for (const agent of agents) {
    const item = idToItem.get(agent.id)!;
    const parentId = agent.parent_agent_id;
    const parentItem = parentId ? idToItem.get(parentId) : undefined;

    if (parentItem) {
      parentItem.children.push(item);
    } else {
      roots.push(item);
    }
  }

  // Update collapsible state now that children are populated
  for (const item of idToItem.values()) {
    item.updateCollapsibleState();
  }

  // Stable sort: roots by spawn_ts ascending (oldest / lead agent first)
  roots.sort((a, b) => (a.agent.spawn_ts ?? 0) - (b.agent.spawn_ts ?? 0));

  return roots;
}
