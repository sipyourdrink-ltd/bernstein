import * as vscode from 'vscode';
import type { BernsteinTask } from './BernsteinClient';

const STATUS_ICON: Record<string, string> = {
  open: '$(circle-outline)',
  claimed: '$(sync~spin)',
  in_progress: '$(sync~spin)',
  done: '$(check)',
  failed: '$(error)',
  blocked: '$(warning)',
  cancelled: '$(x)',
};

export class TaskItem extends vscode.TreeItem {
  constructor(public readonly task: BernsteinTask) {
    const icon = STATUS_ICON[task.status] ?? '$(circle-outline)';
    super(`${icon} ${task.title}`, vscode.TreeItemCollapsibleState.None);

    let desc = task.role;
    if (task.assigned_agent) {
      desc = `${task.role} • ${task.assigned_agent.slice(0, 12)}`;
    }
    if (task.progress !== undefined && task.progress > 0) {
      desc += ` ${task.progress}%`;
    }
    this.description = desc;

    this.tooltip = [
      task.title,
      `Status: ${task.status}`,
      `Role: ${task.role}`,
      task.assigned_agent ? `Agent: ${task.assigned_agent}` : null,
      task.progress ? `Progress: ${task.progress}%` : null,
    ]
      .filter(Boolean)
      .join('\n');

    this.contextValue = `task.${task.status}`;

    // Click → show assigned agent's output channel (if claimed/in-progress)
    if (task.assigned_agent) {
      this.command = {
        command: 'bernstein.openTask',
        title: 'Show Agent Output',
        arguments: [this],
      };
    }
  }
}

export class TaskTreeProvider implements vscode.TreeDataProvider<TaskItem> {
  private readonly _onDidChangeTreeData =
    new vscode.EventEmitter<TaskItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private tasks: BernsteinTask[] = [];

  update(tasks: BernsteinTask[]): void {
    this.tasks = tasks;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: TaskItem): vscode.TreeItem {
    return element;
  }

  getChildren(): TaskItem[] {
    return this.tasks.map((t) => new TaskItem(t));
  }
}
