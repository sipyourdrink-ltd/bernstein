import * as vscode from 'vscode';
import { BernsteinClient } from './BernsteinClient';
import { TaskTreeProvider } from './TaskTreeProvider';
import { AgentTreeProvider } from './AgentTreeProvider';
import { StatusBarManager } from './StatusBarManager';
import { OutputManager } from './OutputManager';
import { DashboardProvider } from './DashboardProvider';
import { registerCommands } from './commands';

type StopFn = () => void;
let stopSse: StopFn | undefined;

/** Called by VS Code when the extension activates. */
export function activate(context: vscode.ExtensionContext): void {
  console.log('Bernstein extension activated');
  const config = vscode.workspace.getConfiguration('bernstein');
  const baseUrl = config.get<string>('apiUrl', 'http://127.0.0.1:8052');
  const token = config.get<string>('apiToken', '');
  const refreshIntervalSecs = config.get<number>('refreshInterval', 5);

  const client = new BernsteinClient(baseUrl, token);
  const taskProvider = new TaskTreeProvider();
  const agentProvider = new AgentTreeProvider();
  const statusBar = new StatusBarManager();
  const outputManager = new OutputManager();
  const dashboardProvider = new DashboardProvider();

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('bernstein.agents', agentProvider),
    vscode.window.registerTreeDataProvider('bernstein.tasks', taskProvider),
    vscode.window.registerWebviewViewProvider('bernstein.dashboard', dashboardProvider),
    { dispose: () => statusBar.dispose() },
    { dispose: () => outputManager.dispose() },
  );

  let hasShownCostWarning = false;
  let hasShownCostStop = false;

  const refresh = async (): Promise<void> => {
    try {
      const data = await client.getDashboardData();
      taskProvider.update(data.tasks);
      agentProvider.update(data.agents);
      statusBar.update(data);
      dashboardProvider.update(data);

      // Cost budget notifications (show once per condition)
      if (data.live_costs.should_stop) {
        if (!hasShownCostStop) {
          hasShownCostStop = true;
          void vscode.window.showErrorMessage(
            `Bernstein: Cost budget exceeded — $${data.live_costs.spent_usd.toFixed(2)} / $${data.live_costs.budget_usd.toFixed(2)} (${data.live_costs.percentage_used.toFixed(0)}%). Agents should be stopped.`,
          );
        }
      } else {
        hasShownCostStop = false;
      }

      if (data.live_costs.should_warn) {
        if (!hasShownCostWarning) {
          hasShownCostWarning = true;
          void vscode.window.showWarningMessage(
            `Bernstein: Cost budget warning — $${data.live_costs.spent_usd.toFixed(2)} / $${data.live_costs.budget_usd.toFixed(2)} (${data.live_costs.percentage_used.toFixed(0)}% used).`,
          );
        }
      } else {
        hasShownCostWarning = false;
      }
    } catch (e) {
      statusBar.setError(String(e));
    }
  };

  // Initial fetch + polling fallback
  void refresh();
  const timer = setInterval(() => void refresh(), refreshIntervalSecs * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  // Debounced refresh — max 2 updates per second from SSE events
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;
  const debouncedRefresh = (): void => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => void refresh(), 500);
  };
  context.subscriptions.push({ dispose: () => clearTimeout(debounceTimer) });

  // SSE real-time updates
  stopSse = client.subscribeToEvents(
    (event, rawData) => {
      if (event === 'task_update' || event === 'agent_update') {
        debouncedRefresh();
      }
      if (event === 'agent_output') {
        try {
          const parsed = JSON.parse(rawData) as { agent_id?: string; line?: string };
          if (parsed.agent_id && parsed.line) {
            outputManager.appendLine(parsed.agent_id, parsed.line);
          }
        } catch {
          // Malformed SSE data — ignore
        }
      }
    },
    () => { /* BernsteinClient reconnects automatically on SSE error */ },
  );
  context.subscriptions.push({ dispose: () => stopSse?.() });

  registerCommands(context, client, outputManager, () => void refresh());

  // @bernstein chat participant — guarded for older VS Code versions
  const vscodeAny = vscode as unknown as {
    chat?: {
      createChatParticipant: (
        id: string,
        handler: (
          req: vscode.ChatRequest,
          ctx: vscode.ChatContext,
          stream: vscode.ChatResponseStream,
        ) => Promise<void>,
      ) => vscode.Disposable;
    };
  };

  if (vscodeAny.chat?.createChatParticipant) {
    const participant = vscodeAny.chat.createChatParticipant(
      'bernstein.chat',
      async (req, _ctx, stream) => {
        const q = req.prompt.trim().toLowerCase();
        try {
          const data = await client.getDashboardData();
          if (q === '' || q.startsWith('status')) {
            stream.markdown(
              `**Bernstein Status**\n\n` +
              `- Agents active: ${data.stats.agents}\n` +
              `- Open tasks: ${data.stats.open}\n` +
              `- Running: ${data.stats.claimed}\n` +
              `- Done: ${data.stats.done}\n` +
              `- Total cost: $${data.stats.cost_usd.toFixed(2)}`,
            );
          } else if (q.startsWith('cost')) {
            stream.markdown(
              `**Cost Summary**\n\nTotal: $${data.live_costs.spent_usd.toFixed(4)}`,
            );
            if (data.live_costs.per_model) {
              const rows = Object.entries(data.live_costs.per_model)
                .map(([m, c]) => `- ${m}: $${(c as number).toFixed(4)}`)
                .join('\n');
              stream.markdown(`\n\n**By model:**\n${rows}`);
            }
          } else {
            stream.markdown(
              `Available commands: \`status\`, \`costs\`\n\n` +
              `For full control, open the [Bernstein Dashboard](${baseUrl}/dashboard).`,
            );
          }
        } catch (e) {
          stream.markdown(`Bernstein is offline: ${String(e)}`);
        }
      },
    );
    context.subscriptions.push(participant);
  }
}

/** Called by VS Code when the extension deactivates. */
export function deactivate(): void {
  stopSse?.();
}
