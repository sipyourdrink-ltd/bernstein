import * as vscode from 'vscode';
import { registerCommands } from '../commands';
import type { BernsteinClient } from '../BernsteinClient';
import type { OutputManager } from '../OutputManager';
import type { AgentItem } from '../AgentTreeProvider';
import type { BernsteinAgent } from '../BernsteinClient';

const BASE_AGENT: BernsteinAgent = {
  id: 'backend-abc123def456',
  role: 'backend',
  status: 'working',
  cost_usd: 0.1234,
  runtime_s: 90,
  model: 'sonnet',
  tasks: [{ id: 't1', title: 'Write tests', status: 'in_progress', progress: 50 }],
};

function makeAgentItem(agent: BernsteinAgent): AgentItem {
  return { agent } as unknown as AgentItem;
}

describe('bernstein.inspectAgent', () => {
  let registeredCommands: Record<string, (...args: unknown[]) => unknown>;
  let mockClient: Partial<BernsteinClient>;
  let mockOutputManager: Partial<OutputManager>;
  let mockContext: Partial<vscode.ExtensionContext>;

  beforeEach(() => {
    registeredCommands = {};
    jest.mocked(vscode.commands.registerCommand).mockImplementation(
      (id: string, handler: (...args: unknown[]) => unknown) => {
        registeredCommands[id] = handler;
        return { dispose: jest.fn() };
      },
    );

    mockClient = {
      baseUrl: 'http://127.0.0.1:8052',
      killAgent: jest.fn(),
      cancelTask: jest.fn(),
      prioritizeTask: jest.fn(),
    };

    mockOutputManager = {
      show: jest.fn(),
    };

    mockContext = {
      subscriptions: [],
    };

    registerCommands(
      mockContext as vscode.ExtensionContext,
      mockClient as BernsteinClient,
      mockOutputManager as OutputManager,
      jest.fn(),
    );
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('registers bernstein.inspectAgent command', () => {
    expect(registeredCommands['bernstein.inspectAgent']).toBeDefined();
  });

  it('calls showInformationMessage with agent id', () => {
    const item = makeAgentItem(BASE_AGENT);
    registeredCommands['bernstein.inspectAgent'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('backend-abc123def456'),
    );
  });

  it('includes role in message', () => {
    const item = makeAgentItem(BASE_AGENT);
    registeredCommands['bernstein.inspectAgent'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('backend'),
    );
  });

  it('includes cost in message', () => {
    const item = makeAgentItem(BASE_AGENT);
    registeredCommands['bernstein.inspectAgent'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('$0.1234'),
    );
  });

  it('includes task titles when agent has tasks', () => {
    const item = makeAgentItem(BASE_AGENT);
    registeredCommands['bernstein.inspectAgent'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('Write tests'),
    );
  });

  it('shows "No tasks" when agent has no tasks', () => {
    const item = makeAgentItem({ ...BASE_AGENT, tasks: [] });
    registeredCommands['bernstein.inspectAgent'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('No tasks'),
    );
  });
});
