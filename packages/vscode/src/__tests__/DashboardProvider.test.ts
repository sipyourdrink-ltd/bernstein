import * as vscode from 'vscode';
import { DashboardProvider, buildHtml } from '../DashboardProvider';
import type { DashboardData } from '../BernsteinClient';

function makeDashboardData(overrides?: Partial<DashboardData>): DashboardData {
  return {
    ts: 1700000000,
    stats: {
      total: 10,
      open: 3,
      claimed: 2,
      done: 4,
      failed: 1,
      agents: 2,
      cost_usd: 1.23,
    },
    tasks: [],
    agents: [],
    live_costs: {
      spent_usd: 1.23,
      budget_usd: 10,
      percentage_used: 12.3,
      should_warn: false,
      should_stop: false,
    },
    alerts: [],
    ...overrides,
  };
}

describe('buildHtml', () => {
  describe('Vector 1: Task breakdown pills', () => {
    it('renders status pills when tasks have various statuses', () => {
      const data = makeDashboardData();
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000005,
        isRefreshing: false,
      });
      expect(html).toContain('pill-open');
      expect(html).toContain('3 open');
      expect(html).toContain('pill-progress');
      expect(html).toContain('2 in progress');
      expect(html).toContain('pill-done');
      expect(html).toContain('4 done');
      expect(html).toContain('pill-failed');
      expect(html).toContain('1 failed');
    });

    it('omits pills for zero-count statuses', () => {
      const data = makeDashboardData({
        stats: {
          total: 5,
          open: 0,
          claimed: 0,
          done: 5,
          failed: 0,
          agents: 1,
          cost_usd: 0.5,
        },
      });
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000005,
        isRefreshing: false,
      });
      // Check body content for actual pill elements (not CSS class definitions)
      expect(html).not.toContain('0 open');
      expect(html).not.toContain('0 in progress');
      expect(html).toContain('5 done');
      expect(html).not.toContain('0 failed');
    });
  });

  describe('Vector 2: Auto-refresh indicator', () => {
    it('shows refresh-dot without refreshing class when not refreshing', () => {
      const data = makeDashboardData();
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000005,
        isRefreshing: false,
      });
      expect(html).toContain('refresh-dot');
      expect(html).not.toContain('refresh-dot refreshing');
    });

    it('shows pulsing refresh-dot when refreshing', () => {
      const data = makeDashboardData();
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000005,
        isRefreshing: true,
      });
      expect(html).toContain('refresh-dot refreshing');
    });

    it('shows "Last updated: just now" when ts is recent', () => {
      const data = makeDashboardData({ ts: 1700000000 });
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000002,
        isRefreshing: false,
      });
      expect(html).toContain('Last updated: just now');
    });

    it('shows "Last updated: Xs ago" for older timestamps', () => {
      const data = makeDashboardData({ ts: 1700000000 });
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000030,
        isRefreshing: false,
      });
      expect(html).toContain('Last updated: 30s ago');
    });

    it('shows "Last updated: Xm ago" for minutes-old data', () => {
      const data = makeDashboardData({ ts: 1700000000 });
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000180,
        isRefreshing: false,
      });
      expect(html).toContain('Last updated: 3m ago');
    });
  });

  describe('Vector 3: Empty states', () => {
    it('shows offline state with Start button when data is null', () => {
      const html = buildHtml({
        data: null,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000000,
        isRefreshing: false,
      });
      expect(html).toContain('Bernstein is not running');
      expect(html).toContain('127.0.0.1:8052');
      expect(html).toContain('startBtn');
      expect(html).toContain('Start Bernstein');
      expect(html).toContain('bernstein.start');
    });

    it('escapes the baseUrl in offline state', () => {
      const html = buildHtml({
        data: null,
        costHistory: [],
        baseUrl: 'http://evil<script>alert(1)</script>',
        nowTs: 1700000000,
        isRefreshing: false,
      });
      expect(html).not.toContain('<script>alert');
      expect(html).toContain('&lt;script&gt;');
    });

    it('shows idle state when connected with 0 agents and 0 tasks', () => {
      const data = makeDashboardData({
        stats: {
          total: 0,
          open: 0,
          claimed: 0,
          done: 0,
          failed: 0,
          agents: 0,
          cost_usd: 0,
        },
        agents: [],
        tasks: [],
      });
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000005,
        isRefreshing: false,
      });
      expect(html).toContain('No active agents');
      expect(html).toContain('bernstein run');
    });

    it('does not show idle state when there are active agents or tasks', () => {
      const data = makeDashboardData();
      const html = buildHtml({
        data,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000005,
        isRefreshing: false,
      });
      expect(html).not.toContain('No active agents');
    });

    it('includes enableScripts in CSP for the start button', () => {
      const html = buildHtml({
        data: null,
        costHistory: [],
        baseUrl: 'http://127.0.0.1:8052',
        nowTs: 1700000000,
        isRefreshing: false,
      });
      expect(html).toContain('script-src');
    });
  });
});

describe('DashboardProvider', () => {
  it('creates with enableScripts true in resolveWebviewView', () => {
    const provider = new DashboardProvider();
    const mockWebview = {
      options: {} as { enableScripts?: boolean },
      html: '',
      onDidReceiveMessage: jest.fn(),
    };
    const mockView = {
      webview: mockWebview,
    } as unknown as vscode.WebviewView;

    provider.resolveWebviewView(mockView);
    expect(mockWebview.options.enableScripts).toBe(true);
  });

  it('handles postMessage for bernstein.start command', () => {
    const provider = new DashboardProvider();
    let messageHandler: ((msg: { command?: string }) => void) | undefined;
    const mockWebview = {
      options: {} as { enableScripts?: boolean },
      html: '',
      onDidReceiveMessage: jest.fn((handler: (msg: { command?: string }) => void) => {
        messageHandler = handler;
      }),
    };
    const mockView = {
      webview: mockWebview,
    } as unknown as vscode.WebviewView;

    provider.resolveWebviewView(mockView);
    expect(messageHandler).toBeDefined();

    // Simulate message from webview
    messageHandler!({ command: 'bernstein.start' });
    expect(vscode.commands.executeCommand).toHaveBeenCalledWith('bernstein.start');
  });

  it('update() adds cost to history and renders', () => {
    const provider = new DashboardProvider();
    const mockWebview = {
      options: {} as { enableScripts?: boolean },
      html: '',
      onDidReceiveMessage: jest.fn(),
    };
    const mockView = {
      webview: mockWebview,
    } as unknown as vscode.WebviewView;

    provider.resolveWebviewView(mockView);
    const data = makeDashboardData();
    provider.update(data);
    expect(mockWebview.html).toContain('4/10');
    expect(mockWebview.html).toContain('$1.23');
  });

  it('setRefreshing(true) shows pulsing dot', () => {
    const provider = new DashboardProvider();
    const mockWebview = {
      options: {} as { enableScripts?: boolean },
      html: '',
      onDidReceiveMessage: jest.fn(),
    };
    const mockView = {
      webview: mockWebview,
    } as unknown as vscode.WebviewView;

    provider.resolveWebviewView(mockView);
    // First provide data so we see the header bar
    provider.update(makeDashboardData());
    provider.setRefreshing(true);
    expect(mockWebview.html).toContain('refresh-dot refreshing');
  });
});
