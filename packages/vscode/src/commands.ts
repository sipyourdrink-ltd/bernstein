import * as vscode from 'vscode';
import type { BernsteinClient } from './BernsteinClient';
import type { AgentItem } from './AgentTreeProvider';
import type { TaskItem } from './TaskTreeProvider';
import type { OutputManager } from './OutputManager';
import { DashboardProvider } from './DashboardProvider';

export function registerCommands(
  context: vscode.ExtensionContext,
  client: BernsteinClient,
  outputManager: OutputManager,
  onRefresh: () => void,
): void {
  context.subscriptions.push(

    vscode.commands.registerCommand('bernstein.start', () => {
      const terminal = vscode.window.createTerminal({ name: 'Bernstein' });
      terminal.show();
      terminal.sendText('bernstein run');
    }),

    vscode.commands.registerCommand('bernstein.refresh', onRefresh),

    vscode.commands.registerCommand('bernstein.showDashboard', () => {
      DashboardProvider.openInBrowser(client.baseUrl);
    }),

    vscode.commands.registerCommand(
      'bernstein.killAgent',
      async (item: AgentItem) => {
        const answer = await vscode.window.showWarningMessage(
          `Kill agent ${item.agent.id}?`,
          { modal: true },
          'Kill',
        );
        if (answer === 'Kill') {
          try {
            await client.killAgent(item.agent.id);
            vscode.window.showInformationMessage(
              `Kill signal sent to ${item.agent.id}`,
            );
            onRefresh();
          } catch (e) {
            vscode.window.showErrorMessage(`Failed to kill agent: ${String(e)}`);
          }
        }
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.showAgentOutput',
      (item: AgentItem) => {
        outputManager.show(item.agent.id);
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.cancelTask',
      async (item: TaskItem) => {
        const answer = await vscode.window.showWarningMessage(
          `Cancel task "${item.task.title}"?`,
          { modal: true },
          'Cancel Task',
        );
        if (answer === 'Cancel Task') {
          try {
            await client.cancelTask(item.task.id);
            vscode.window.showInformationMessage(`Task "${item.task.title}" cancelled.`);
            onRefresh();
          } catch (e) {
            vscode.window.showErrorMessage(`Failed to cancel task: ${String(e)}`);
          }
        }
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.openTask',
      (item: TaskItem) => {
        if (item.task.assigned_agent) {
          outputManager.show(item.task.assigned_agent);
        }
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.prioritizeTask',
      async (item: TaskItem) => {
        try {
          await client.prioritizeTask(item.task.id);
          vscode.window.showInformationMessage(`Task "${item.task.title}" moved to top of queue.`);
          onRefresh();
        } catch (e) {
          vscode.window.showErrorMessage(`Failed to prioritize task: ${String(e)}`);
        }
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.approveTask',
      async (item: TaskItem) => {
        try {
          await client.approveTask(item.task.id);
          vscode.window.showInformationMessage(`Task "${item.task.title}" approved.`);
          onRefresh();
        } catch (e) {
          vscode.window.showErrorMessage(`Failed to approve task: ${String(e)}`);
        }
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.rejectTask',
      async (item: TaskItem) => {
        const answer = await vscode.window.showWarningMessage(
          `Reject task "${item.task.title}"?`,
          { modal: true },
          'Reject',
        );
        if (answer === 'Reject') {
          try {
            await client.rejectTask(item.task.id);
            vscode.window.showInformationMessage(`Task "${item.task.title}" rejected.`);
            onRefresh();
          } catch (e) {
            vscode.window.showErrorMessage(`Failed to reject task: ${String(e)}`);
          }
        }
      },
    ),

    vscode.commands.registerCommand(
      'bernstein.inspectAgent',
      (item: AgentItem) => {
        const a = item.agent;
        const runtime =
          a.runtime_s > 60
            ? `${Math.floor(a.runtime_s / 60)}m`
            : `${a.runtime_s}s`;
        const taskList = a.tasks?.length
          ? a.tasks.map((t) => t.title).join(', ')
          : 'No tasks';
        const msg = [
          `Agent: ${a.id}`,
          `Role: ${a.role}`,
          `Model: ${a.model ?? 'unknown'}`,
          `Status: ${a.status}`,
          `Runtime: ${runtime}`,
          `Cost: $${a.cost_usd.toFixed(4)}`,
          `Tasks: ${taskList}`,
        ].join(' | ');
        void vscode.window.showInformationMessage(msg);
      },
    ),

  );
}
