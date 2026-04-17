import * as vscode from 'vscode';
import { registerCommands } from '../commands';
import type { BernsteinClient, BernsteinAgent, BernsteinTask } from '../BernsteinClient';
import type { OutputManager } from '../OutputManager';
import type { AgentItem } from '../AgentTreeProvider';
import type { TaskItem } from '../TaskTreeProvider';

function makeAgentItem(agent: BernsteinAgent): AgentItem {
  return { agent } as unknown as AgentItem;
}

function makeTaskItem(task: BernsteinTask): TaskItem {
  return { task } as unknown as TaskItem;
}

const BASE_AGENT: BernsteinAgent = {
  id: 'backend-abc123def456',
  role: 'backend',
  status: 'working',
  cost_usd: 0.1234,
  runtime_s: 90,
  model: 'sonnet',
  tasks: [{ id: 't1', title: 'Write tests', status: 'in_progress', progress: 50 }],
};

const BASE_TASK: BernsteinTask = {
  id: 'task-001',
  title: 'Implement login',
  role: 'backend',
  status: 'pending_approval',
  priority: 1,
};

describe('bernstein.inspectAgent', () => {
  let registeredCommands: Record<string, (...args: unknown[]) => unknown>;
  let mockClient: BernsteinClient;
  let mockOutputManager: OutputManager;
  let mockContext: vscode.ExtensionContext;

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
      approveTask: jest.fn(),
      rejectTask: jest.fn(),
    } as unknown as BernsteinClient;

    mockOutputManager = {
      show: jest.fn(),
    } as unknown as OutputManager;

    mockContext = {
      subscriptions: [],
    } as unknown as vscode.ExtensionContext;

    registerCommands(
      mockContext,
      mockClient,
      mockOutputManager,
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

describe('bernstein.approveTask', () => {
  let registeredCommands: Record<string, (...args: unknown[]) => unknown>;
  let mockClient: BernsteinClient;
  let mockOutputManager: OutputManager;
  let mockContext: vscode.ExtensionContext;
  let mockRefresh: jest.Mock;

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
      approveTask: jest.fn().mockResolvedValue(undefined),
      rejectTask: jest.fn(),
    } as unknown as BernsteinClient;

    mockOutputManager = { show: jest.fn() } as unknown as OutputManager;
    mockContext = { subscriptions: [] } as unknown as vscode.ExtensionContext;
    mockRefresh = jest.fn();

    registerCommands(
      mockContext,
      mockClient,
      mockOutputManager,
      mockRefresh,
    );
  });

  afterEach(() => { jest.clearAllMocks(); });

  it('registers bernstein.approveTask command', () => {
    expect(registeredCommands['bernstein.approveTask']).toBeDefined();
  });

  it('calls client.approveTask with the task id', async () => {
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.approveTask'](item);
    expect(mockClient.approveTask).toHaveBeenCalledWith('task-001');
  });

  it('shows success message after approval', async () => {
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.approveTask'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('approved'),
    );
  });

  it('triggers refresh after approval', async () => {
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.approveTask'](item);
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('shows error message when approval fails', async () => {
    (mockClient.approveTask as jest.Mock).mockRejectedValue(new Error('HTTP 500'));
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.approveTask'](item);
    expect(vscode.window.showErrorMessage).toHaveBeenCalledWith(
      expect.stringContaining('Failed to approve task'),
    );
  });
});

describe('bernstein.rejectTask', () => {
  let registeredCommands: Record<string, (...args: unknown[]) => unknown>;
  let mockClient: BernsteinClient;
  let mockOutputManager: OutputManager;
  let mockContext: vscode.ExtensionContext;
  let mockRefresh: jest.Mock;

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
      approveTask: jest.fn(),
      rejectTask: jest.fn().mockResolvedValue(undefined),
    } as unknown as BernsteinClient;

    mockOutputManager = { show: jest.fn() } as unknown as OutputManager;
    mockContext = { subscriptions: [] } as unknown as vscode.ExtensionContext;
    mockRefresh = jest.fn();

    registerCommands(
      mockContext,
      mockClient,
      mockOutputManager,
      mockRefresh,
    );
  });

  afterEach(() => { jest.clearAllMocks(); });

  it('registers bernstein.rejectTask command', () => {
    expect(registeredCommands['bernstein.rejectTask']).toBeDefined();
  });

  it('calls client.rejectTask when user confirms', async () => {
    jest.mocked(vscode.window.showWarningMessage).mockResolvedValue('Reject' as never);
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.rejectTask'](item);
    expect(mockClient.rejectTask).toHaveBeenCalledWith('task-001');
  });

  it('does not call client.rejectTask when user cancels dialog', async () => {
    jest.mocked(vscode.window.showWarningMessage).mockResolvedValue(undefined as never);
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.rejectTask'](item);
    expect(mockClient.rejectTask).not.toHaveBeenCalled();
  });

  it('shows success message after rejection', async () => {
    jest.mocked(vscode.window.showWarningMessage).mockResolvedValue('Reject' as never);
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.rejectTask'](item);
    expect(vscode.window.showInformationMessage).toHaveBeenCalledWith(
      expect.stringContaining('rejected'),
    );
  });

  it('triggers refresh after rejection', async () => {
    jest.mocked(vscode.window.showWarningMessage).mockResolvedValue('Reject' as never);
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.rejectTask'](item);
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('shows error message when rejection fails', async () => {
    jest.mocked(vscode.window.showWarningMessage).mockResolvedValue('Reject' as never);
    (mockClient.rejectTask as jest.Mock).mockRejectedValue(new Error('HTTP 500'));
    const item = makeTaskItem(BASE_TASK);
    await registeredCommands['bernstein.rejectTask'](item);
    expect(vscode.window.showErrorMessage).toHaveBeenCalledWith(
      expect.stringContaining('Failed to reject task'),
    );
  });
});
