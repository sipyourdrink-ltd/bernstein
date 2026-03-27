import * as vscode from 'vscode';

export class OutputManager {
  private readonly channels = new Map<string, vscode.OutputChannel>();

  private getOrCreate(agentId: string): vscode.OutputChannel {
    if (!this.channels.has(agentId)) {
      this.channels.set(
        agentId,
        vscode.window.createOutputChannel(`Bernstein: ${agentId}`),
      );
    }
    return this.channels.get(agentId)!;
  }

  appendLine(agentId: string, line: string): void {
    this.getOrCreate(agentId).appendLine(line);
  }

  show(agentId: string): void {
    this.getOrCreate(agentId).show(true);
  }

  dispose(): void {
    for (const channel of this.channels.values()) {
      channel.dispose();
    }
    this.channels.clear();
  }
}
