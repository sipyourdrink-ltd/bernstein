# VS Code / Cursor Extension (340b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a VS Code/Cursor extension that shows Bernstein agents and tasks in a sidebar, tracks live costs in the status bar, and supports real-time SSE updates.

**Architecture:** TypeScript extension (Node.js host) connecting to Bernstein's HTTP API at `localhost:8052`. Tree views for agents and tasks, a webview sidebar for the overview panel, a status bar item, per-agent output channels, and a `@bernstein` chat participant. No external runtime dependencies — uses Node.js built-in `http`/`https` modules.

**Tech Stack:** TypeScript 5.4, VS Code API 1.90+, esbuild (bundle), Jest + ts-jest (unit tests), `@vscode/vsce` (package/publish)

---

## File Map

```
packages/vscode/
├── package.json                          # Extension manifest, contributes, scripts
├── tsconfig.json                         # TS config (CommonJS output)
├── esbuild.mjs                           # Build script (bundles src/ → dist/extension.js)
├── jest.config.js                        # Jest config with vscode mock
├── .vscodeignore                         # Excludes src/, tests from VSIX
├── media/
│   └── bernstein-icon.svg                # Musical note SVG for activity bar
├── src/
│   ├── __mocks__/
│   │   └── vscode.ts                     # Jest mock for 'vscode' module
│   ├── __tests__/
│   │   ├── BernsteinClient.test.ts       # Unit tests: URL, headers, parsing
│   │   ├── TaskTreeProvider.test.ts      # Unit tests: item creation, labels
│   │   └── AgentTreeProvider.test.ts     # Unit tests: status icons, cost display
│   ├── BernsteinClient.ts               # HTTP GET/POST + SSE stream client
│   ├── TaskTreeProvider.ts              # TreeDataProvider<TaskItem>
│   ├── AgentTreeProvider.ts             # TreeDataProvider<AgentItem>
│   ├── StatusBarManager.ts              # Status bar: "3 agents | 5/12 tasks | $0.42"
│   ├── OutputManager.ts                 # Per-agent OutputChannels
│   ├── DashboardProvider.ts             # WebviewViewProvider (sidebar) + openFullDashboard()
│   ├── commands.ts                      # registerCommands() helper
│   └── extension.ts                     # activate() / deactivate()

.github/workflows/
└── publish-extension.yml                # Publishes to VS Code Marketplace + Open VSX on tag
```

---

## Task 1: Package scaffold

**Files:**
- Create: `packages/vscode/package.json`
- Create: `packages/vscode/tsconfig.json`
- Create: `packages/vscode/esbuild.mjs`
- Create: `packages/vscode/jest.config.js`
- Create: `packages/vscode/.vscodeignore`
- Create: `packages/vscode/media/bernstein-icon.svg`

- [ ] **Step 1: Create package.json**

```json
{
  "name": "bernstein",
  "displayName": "Bernstein",
  "description": "Multi-agent orchestration — monitor agents, track tasks, and control your team from VS Code",
  "version": "0.1.0",
  "publisher": "bernstein",
  "engines": { "vscode": "^1.90.0" },
  "categories": ["Other"],
  "icon": "media/bernstein-icon.svg",
  "repository": { "type": "git", "url": "https://github.com/YOUR_ORG/bernstein" },
  "license": "MIT",
  "activationEvents": ["onStartupFinished"],
  "main": "./dist/extension.js",
  "contributes": {
    "configuration": {
      "title": "Bernstein",
      "properties": {
        "bernstein.apiUrl": {
          "type": "string",
          "default": "http://127.0.0.1:8052",
          "description": "Bernstein orchestrator API URL"
        },
        "bernstein.apiToken": {
          "type": "string",
          "default": "",
          "description": "Bearer token for Bernstein API (optional)"
        },
        "bernstein.refreshInterval": {
          "type": "number",
          "default": 5,
          "description": "Tree view refresh interval in seconds"
        }
      }
    },
    "viewsContainers": {
      "activitybar": [
        { "id": "bernstein", "title": "Bernstein", "icon": "media/bernstein-icon.svg" }
      ]
    },
    "views": {
      "bernstein": [
        { "id": "bernstein.agents", "name": "Agents" },
        { "id": "bernstein.tasks", "name": "Tasks" },
        { "id": "bernstein.dashboard", "name": "Overview", "type": "webview" }
      ]
    },
    "commands": [
      { "command": "bernstein.refresh", "title": "Bernstein: Refresh", "icon": "$(refresh)" },
      { "command": "bernstein.showDashboard", "title": "Bernstein: Show Dashboard", "icon": "$(browser)" },
      { "command": "bernstein.killAgent", "title": "Bernstein: Kill Agent", "icon": "$(stop)" },
      { "command": "bernstein.showAgentOutput", "title": "Bernstein: Show Agent Output", "icon": "$(output)" }
    ],
    "menus": {
      "view/title": [
        { "command": "bernstein.refresh", "when": "view == bernstein.agents || view == bernstein.tasks", "group": "navigation" },
        { "command": "bernstein.showDashboard", "when": "view == bernstein.agents", "group": "navigation" }
      ],
      "view/item/context": [
        { "command": "bernstein.killAgent", "when": "view == bernstein.agents && viewItem == agent.active", "group": "inline" },
        { "command": "bernstein.showAgentOutput", "when": "view == bernstein.agents && viewItem == agent.active", "group": "inline" }
      ]
    },
    "chatParticipants": [
      {
        "id": "bernstein.chat",
        "fullName": "Bernstein Orchestrator",
        "name": "bernstein",
        "description": "Query Bernstein orchestrator status and control agents",
        "isSticky": false
      }
    ]
  },
  "scripts": {
    "compile": "node esbuild.mjs",
    "watch": "node esbuild.mjs --watch",
    "test": "npx jest",
    "package": "npm run compile && vsce package --no-dependencies",
    "lint": "tsc --noEmit"
  },
  "devDependencies": {
    "@types/jest": "^29.5",
    "@types/node": "^20",
    "@types/vscode": "^1.90.0",
    "@vscode/vsce": "^2.26",
    "esbuild": "^0.21",
    "jest": "^29.7",
    "ts-jest": "^29.1",
    "typescript": "^5.4"
  }
}
```

- [ ] **Step 2: Create tsconfig.json**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "commonjs",
    "lib": ["ES2022"],
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["src/**/*"],
  "exclude": ["node_modules", "dist", "**/*.test.ts", "src/__mocks__/**"]
}
```

- [ ] **Step 3: Create esbuild.mjs**

```js
import * as esbuild from 'esbuild';

const watch = process.argv.includes('--watch');

const ctx = await esbuild.context({
  entryPoints: ['src/extension.ts'],
  bundle: true,
  outfile: 'dist/extension.js',
  external: ['vscode'],
  format: 'cjs',
  platform: 'node',
  target: 'node18',
  sourcemap: true,
});

if (watch) {
  await ctx.watch();
  console.log('Watching...');
} else {
  await ctx.rebuild();
  await ctx.dispose();
}
```

- [ ] **Step 4: Create jest.config.js**

```js
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/src/__tests__/**/*.test.ts'],
  moduleNameMapper: {
    vscode: '<rootDir>/src/__mocks__/vscode.ts',
  },
};
```

- [ ] **Step 5: Create .vscodeignore**

```
**/.git/**
**/.gitignore
**/*.ts
!dist/**/*.d.ts
**/*.map
**/.vscode/**
**/node_modules/**
**/src/**
**/esbuild.mjs
**/jest.config.js
**/tsconfig.json
**/.vscodeignore
```

- [ ] **Step 6: Create media/bernstein-icon.svg** (musical note — Bernstein → Leonard Bernstein → music)

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9 18V5l12-2v13"/>
  <circle cx="6" cy="18" r="3"/>
  <circle cx="18" cy="16" r="3"/>
</svg>
```

- [ ] **Step 7: Install dependencies**

Run from `packages/vscode/`:
```bash
cd packages/vscode && npm install
```

Expected: `node_modules/` created, no errors.

- [ ] **Step 8: Commit**

```bash
git add packages/vscode/
git commit -m "feat(vscode): extension scaffold — package.json, build, test tooling"
```

---

## Task 2: vscode mock + BernsteinClient

**Files:**
- Create: `packages/vscode/src/__mocks__/vscode.ts`
- Create: `packages/vscode/src/BernsteinClient.ts`

- [ ] **Step 1: Create `src/__mocks__/vscode.ts`**

This mock lets Jest test TypeScript that imports `vscode` without launching VS Code.

```typescript
export const window = {
  createOutputChannel: jest.fn(() => ({
    appendLine: jest.fn(),
    show: jest.fn(),
    dispose: jest.fn(),
  })),
  createStatusBarItem: jest.fn(() => ({
    text: '',
    tooltip: '',
    command: '',
    show: jest.fn(),
    hide: jest.fn(),
    dispose: jest.fn(),
  })),
  showErrorMessage: jest.fn(),
  showInformationMessage: jest.fn(),
  showWarningMessage: jest.fn(),
  createWebviewPanel: jest.fn(),
  registerWebviewViewProvider: jest.fn(),
  registerTreeDataProvider: jest.fn(),
};

export const workspace = {
  getConfiguration: jest.fn(() => ({
    get: jest.fn((_key: string, def: unknown) => def),
  })),
};

export const commands = {
  registerCommand: jest.fn(),
};

export const StatusBarAlignment = { Left: 1, Right: 2 };

export const TreeItemCollapsibleState = { None: 0, Collapsed: 1, Expanded: 2 };

export class TreeItem {
  label: string;
  description?: string;
  tooltip?: string;
  contextValue?: string;
  collapsibleState: number;
  constructor(label: string, collapsibleState: number = 0) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}

export class EventEmitter {
  event = jest.fn();
  fire = jest.fn();
  dispose = jest.fn();
}

export const ThemeIcon = class ThemeIconMock {
  constructor(public id: string) {}
};

export const ThemeColor = class ThemeColorMock {
  constructor(public id: string) {}
};

export const Uri = {
  parse: jest.fn((s: string) => ({ toString: () => s })),
  joinPath: jest.fn(),
};

export const ViewColumn = { One: 1, Two: 2, Beside: -2 };

export const env = {
  openExternal: jest.fn(),
};
```

- [ ] **Step 2: Create `src/BernsteinClient.ts`**

No external dependencies — uses Node.js built-in `http`/`https`:

```typescript
import * as http from 'http';
import * as https from 'https';

export interface BernsteinStatus {
  total: number;
  open: number;
  claimed: number;
  done: number;
  failed: number;
  total_cost_usd: number;
}

export interface BernsteinTask {
  id: string;
  title: string;
  role: string;
  status: string;
  priority: number;
  agent_id?: string;
  cost_usd?: number;
  progress_pct?: number;
}

export interface BernsteinAgent {
  id: string;
  role: string;
  status: string;
  current_task?: string;
  cost_usd: number;
  runtime_s: number;
  model?: string;
}

export interface DashboardData {
  ts: number;
  stats: {
    open: number;
    claimed: number;
    done: number;
    failed: number;
    total_cost_usd: number;
    agent_count: number;
  };
  tasks: BernsteinTask[];
  agents: BernsteinAgent[];
  live_costs: {
    total_usd: number;
    by_agent?: Record<string, number>;
    by_model?: Record<string, number>;
  };
  alerts: Array<{ level: string; message: string; detail?: string }>;
}

export type SseCallback = (event: string, data: string) => void;

export class BernsteinClient {
  readonly baseUrl: string;
  private readonly token: string;

  constructor(baseUrl: string, token: string = '') {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.token = token;
  }

  headers(): Record<string, string> {
    const h: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.token) {
      h['Authorization'] = `Bearer ${this.token}`;
    }
    return h;
  }

  get<T>(path: string): Promise<T> {
    return new Promise((resolve, reject) => {
      const url = new URL(this.baseUrl + path);
      const lib = url.protocol === 'https:' ? https : http;
      const req = lib.get(url.toString(), { headers: this.headers() }, (res) => {
        let body = '';
        res.on('data', (chunk: Buffer) => { body += chunk.toString(); });
        res.on('end', () => {
          if (res.statusCode !== undefined && res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}: ${body}`));
            return;
          }
          try {
            resolve(JSON.parse(body) as T);
          } catch {
            reject(new Error(`Failed to parse JSON: ${body.slice(0, 200)}`));
          }
        });
      });
      req.on('error', reject);
      req.setTimeout(5000, () => { req.destroy(new Error('Request timeout')); });
    });
  }

  post(path: string, body: unknown = {}): Promise<void> {
    return new Promise((resolve, reject) => {
      const url = new URL(this.baseUrl + path);
      const lib = url.protocol === 'https:' ? https : http;
      const payload = JSON.stringify(body);
      const options = {
        method: 'POST',
        headers: { ...this.headers(), 'Content-Length': Buffer.byteLength(payload) },
      };
      const req = lib.request(url.toString(), options, (res) => {
        res.on('data', () => {});
        res.on('end', () => {
          if (res.statusCode !== undefined && res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}`));
            return;
          }
          resolve();
        });
      });
      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }

  getStatus(): Promise<BernsteinStatus> {
    return this.get<BernsteinStatus>('/status');
  }

  getDashboardData(): Promise<DashboardData> {
    return this.get<DashboardData>('/dashboard/data');
  }

  killAgent(sessionId: string): Promise<void> {
    return this.post(`/agents/${sessionId}/kill`);
  }

  /**
   * Subscribe to SSE /events stream.
   * Returns a stop function — call it to disconnect.
   * Automatically reconnects on error/close.
   */
  subscribeToEvents(onEvent: SseCallback, onError?: (err: Error) => void): () => void {
    const url = new URL(this.baseUrl + '/events');
    const lib = url.protocol === 'https:' ? https : http;
    let aborted = false;

    const connect = (): void => {
      if (aborted) return;
      const req = lib.get(
        url.toString(),
        { headers: { ...this.headers(), Accept: 'text/event-stream' } },
        (res) => {
          let buffer = '';
          let eventName = 'message';

          res.on('data', (chunk: Buffer) => {
            buffer += chunk.toString();
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';
            for (const line of lines) {
              if (line.startsWith('event: ')) {
                eventName = line.slice(7).trim();
              } else if (line.startsWith('data: ')) {
                onEvent(eventName, line.slice(6).trim());
                eventName = 'message';
              }
            }
          });

          res.on('end', () => {
            if (!aborted) setTimeout(connect, 3000);
          });
        },
      );

      req.on('error', (err: Error) => {
        if (!aborted) {
          onError?.(err);
          setTimeout(connect, 5000);
        }
      });
    };

    connect();
    return () => { aborted = true; };
  }
}
```

- [ ] **Step 3: Write failing tests — `src/__tests__/BernsteinClient.test.ts`**

```typescript
import { BernsteinClient } from '../BernsteinClient';

describe('BernsteinClient', () => {
  describe('constructor', () => {
    it('strips trailing slash from baseUrl', () => {
      const client = new BernsteinClient('http://localhost:8052/', '');
      expect(client.baseUrl).toBe('http://localhost:8052');
    });

    it('preserves baseUrl without trailing slash', () => {
      const client = new BernsteinClient('http://localhost:8052', '');
      expect(client.baseUrl).toBe('http://localhost:8052');
    });
  });

  describe('headers()', () => {
    it('includes Authorization when token provided', () => {
      const client = new BernsteinClient('http://localhost:8052', 'mytoken');
      expect(client.headers()['Authorization']).toBe('Bearer mytoken');
    });

    it('omits Authorization when token is empty', () => {
      const client = new BernsteinClient('http://localhost:8052', '');
      expect(client.headers()['Authorization']).toBeUndefined();
    });

    it('always includes Content-Type', () => {
      const client = new BernsteinClient('http://localhost:8052', '');
      expect(client.headers()['Content-Type']).toBe('application/json');
    });
  });
});
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd packages/vscode && npm test
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/vscode/src/
git commit -m "feat(vscode): BernsteinClient — HTTP/SSE client with no external deps"
```

---

## Task 3: TaskTreeProvider

**Files:**
- Create: `packages/vscode/src/TaskTreeProvider.ts`
- Create: `packages/vscode/src/__tests__/TaskTreeProvider.test.ts`

- [ ] **Step 1: Write failing tests — `src/__tests__/TaskTreeProvider.test.ts`**

```typescript
import { TaskItem, TaskTreeProvider } from '../TaskTreeProvider';
import type { BernsteinTask } from '../BernsteinClient';

const BASE_TASK: BernsteinTask = {
  id: 'abc123',
  title: 'Fix auth bug',
  role: 'backend',
  status: 'claimed',
  priority: 1,
};

describe('TaskItem', () => {
  it('sets contextValue based on status', () => {
    const item = new TaskItem({ ...BASE_TASK, status: 'done' });
    expect(item.contextValue).toBe('task.done');
  });

  it('shows progress percent when non-zero', () => {
    const item = new TaskItem({ ...BASE_TASK, progress_pct: 42 });
    expect(item.description).toContain('42%');
  });

  it('shows agent id prefix in description when present', () => {
    const item = new TaskItem({ ...BASE_TASK, agent_id: 'backend-abc123def456' });
    expect(item.description).toContain('backend-abc');
  });

  it('shows only role in description when no agent', () => {
    const item = new TaskItem(BASE_TASK);
    expect(item.description).toBe('backend');
  });

  it('does not show 0% progress', () => {
    const item = new TaskItem({ ...BASE_TASK, progress_pct: 0 });
    expect(item.description).not.toContain('%');
  });
});

describe('TaskTreeProvider', () => {
  it('returns one TaskItem per task', () => {
    const provider = new TaskTreeProvider();
    provider.update([BASE_TASK, { ...BASE_TASK, id: 'def456', title: 'Add tests' }]);
    expect(provider.getChildren()).toHaveLength(2);
  });

  it('returns empty array when no tasks', () => {
    const provider = new TaskTreeProvider();
    expect(provider.getChildren()).toHaveLength(0);
  });

  it('getTreeItem returns the item itself', () => {
    const provider = new TaskTreeProvider();
    const item = new TaskItem(BASE_TASK);
    expect(provider.getTreeItem(item)).toBe(item);
  });
});
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd packages/vscode && npm test -- --testPathPattern=TaskTreeProvider
```

Expected: FAIL — `TaskTreeProvider` not found.

- [ ] **Step 3: Implement `src/TaskTreeProvider.ts`**

```typescript
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
    if (task.agent_id) {
      desc = `${task.role} • ${task.agent_id.slice(0, 12)}`;
    }
    if (task.progress_pct !== undefined && task.progress_pct > 0) {
      desc += ` ${task.progress_pct}%`;
    }
    this.description = desc;

    this.tooltip = [
      task.title,
      `Status: ${task.status}`,
      `Role: ${task.role}`,
      task.cost_usd ? `Cost: $${task.cost_usd.toFixed(4)}` : null,
    ].filter(Boolean).join('\n');

    this.contextValue = `task.${task.status}`;
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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd packages/vscode && npm test -- --testPathPattern=TaskTreeProvider
```

Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/vscode/src/TaskTreeProvider.ts packages/vscode/src/__tests__/TaskTreeProvider.test.ts
git commit -m "feat(vscode): TaskTreeProvider with status icons and progress"
```

---

## Task 4: AgentTreeProvider

**Files:**
- Create: `packages/vscode/src/AgentTreeProvider.ts`
- Create: `packages/vscode/src/__tests__/AgentTreeProvider.test.ts`

- [ ] **Step 1: Write failing tests — `src/__tests__/AgentTreeProvider.test.ts`**

```typescript
import { AgentItem, AgentTreeProvider } from '../AgentTreeProvider';
import type { BernsteinAgent } from '../BernsteinClient';

const BASE_AGENT: BernsteinAgent = {
  id: 'backend-abc123def456',
  role: 'backend',
  status: 'active',
  cost_usd: 0.12,
  runtime_s: 125,
  model: 'sonnet',
};

describe('AgentItem', () => {
  it('sets contextValue to agent.active when status is active', () => {
    const item = new AgentItem(BASE_AGENT);
    expect(item.contextValue).toBe('agent.active');
  });

  it('sets contextValue to agent.active when status is busy', () => {
    const item = new AgentItem({ ...BASE_AGENT, status: 'busy' });
    expect(item.contextValue).toBe('agent.active');
  });

  it('sets contextValue to agent.idle when not active or busy', () => {
    const item = new AgentItem({ ...BASE_AGENT, status: 'idle' });
    expect(item.contextValue).toBe('agent.idle');
  });

  it('shows runtime in minutes when over 60s', () => {
    const item = new AgentItem({ ...BASE_AGENT, runtime_s: 125 });
    expect(item.description).toContain('2m');
  });

  it('shows runtime in seconds when 60s or under', () => {
    const item = new AgentItem({ ...BASE_AGENT, runtime_s: 45 });
    expect(item.description).toContain('45s');
  });

  it('shows cost in description', () => {
    const item = new AgentItem(BASE_AGENT);
    expect(item.description).toContain('$0.12');
  });

  it('shows model in description when present', () => {
    const item = new AgentItem(BASE_AGENT);
    expect(item.description).toContain('sonnet');
  });

  it('falls back to role when model absent', () => {
    const item = new AgentItem({ ...BASE_AGENT, model: undefined });
    expect(item.description).toContain('backend');
  });
});

describe('AgentTreeProvider', () => {
  it('returns one AgentItem per agent', () => {
    const provider = new AgentTreeProvider();
    provider.update([BASE_AGENT, { ...BASE_AGENT, id: 'qa-xyz789' }]);
    expect(provider.getChildren()).toHaveLength(2);
  });

  it('returns empty array when no agents', () => {
    const provider = new AgentTreeProvider();
    expect(provider.getChildren()).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd packages/vscode && npm test -- --testPathPattern=AgentTreeProvider
```

Expected: FAIL — `AgentTreeProvider` not found.

- [ ] **Step 3: Implement `src/AgentTreeProvider.ts`**

```typescript
import * as vscode from 'vscode';
import type { BernsteinAgent } from './BernsteinClient';

const ACTIVE_STATUSES = new Set(['active', 'busy']);

export class AgentItem extends vscode.TreeItem {
  constructor(public readonly agent: BernsteinAgent) {
    const isActive = ACTIVE_STATUSES.has(agent.status);
    const icon = isActive ? '$(circle-filled)' : '$(circle-outline)';
    super(`${icon} ${agent.id.slice(0, 14)}`, vscode.TreeItemCollapsibleState.None);

    const model = agent.model ?? agent.role;
    const runtime = agent.runtime_s > 60
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
    ].filter(Boolean).join('\n');

    this.contextValue = isActive ? 'agent.active' : 'agent.idle';
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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd packages/vscode && npm test -- --testPathPattern=AgentTreeProvider
```

Expected: 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/vscode/src/AgentTreeProvider.ts packages/vscode/src/__tests__/AgentTreeProvider.test.ts
git commit -m "feat(vscode): AgentTreeProvider with status icons, runtime, cost"
```

---

## Task 5: StatusBarManager

**Files:**
- Create: `packages/vscode/src/StatusBarManager.ts`

No unit test needed — it's a thin wrapper over `vscode.window.createStatusBarItem`, which requires VS Code to be running. Covered by visual verification.

- [ ] **Step 1: Create `src/StatusBarManager.ts`**

```typescript
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
    this.item.text = '$(music) Bernstein: connecting…';
    this.item.tooltip = 'Bernstein Orchestrator — click to open dashboard';
    this.item.show();
  }

  update(data: DashboardData): void {
    const { stats } = data;
    const total = stats.done + stats.open + stats.claimed + stats.failed;
    const cost = `$${stats.total_cost_usd.toFixed(2)}`;
    const agents = stats.agent_count;
    const tasks = `${stats.done}/${total}`;
    this.item.text = `$(music) ${agents} agents | ${tasks} tasks | ${cost}`;
    this.item.tooltip =
      `Bernstein — ${agents} active agents, ${tasks} tasks done, ${cost} total cost`;
  }

  setError(message: string): void {
    this.item.text = '$(music) Bernstein: offline';
    this.item.tooltip = `Bernstein: ${message}`;
  }

  dispose(): void {
    this.item.dispose();
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add packages/vscode/src/StatusBarManager.ts
git commit -m "feat(vscode): StatusBarManager — agents | tasks | cost"
```

---

## Task 6: OutputManager

**Files:**
- Create: `packages/vscode/src/OutputManager.ts`

- [ ] **Step 1: Create `src/OutputManager.ts`**

```typescript
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
```

- [ ] **Step 2: Commit**

```bash
git add packages/vscode/src/OutputManager.ts
git commit -m "feat(vscode): OutputManager — per-agent output channels"
```

---

## Task 7: DashboardProvider

**Files:**
- Create: `packages/vscode/src/DashboardProvider.ts`

The sidebar overview panel is a `WebviewViewProvider`. The "Show Dashboard" command opens the full Bernstein web dashboard in the default browser (VS Code webviews can't iframe localhost due to CSP, so we open it externally).

- [ ] **Step 1: Create `src/DashboardProvider.ts`**

```typescript
import * as vscode from 'vscode';
import type { DashboardData } from './BernsteinClient';

function getNonce(): string {
  const chars =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from(
    { length: 32 },
    () => chars[Math.floor(Math.random() * chars.length)],
  ).join('');
}

function buildHtml(data: DashboardData | null): string {
  const nonce = getNonce();
  const csp =
    `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';`;

  const stats = data?.stats;
  const statsHtml = stats
    ? `
      <div class="stat"><span class="label">Agents</span><span class="value">${stats.agent_count}</span></div>
      <div class="stat"><span class="label">Open</span><span class="value open">${stats.open}</span></div>
      <div class="stat"><span class="label">Running</span><span class="value running">${stats.claimed}</span></div>
      <div class="stat"><span class="label">Done</span><span class="value done">${stats.done}</span></div>
      <div class="stat"><span class="label">Failed</span><span class="value failed">${stats.failed}</span></div>
      <div class="stat"><span class="label">Cost</span><span class="value">$${stats.total_cost_usd.toFixed(2)}</span></div>`
    : '<div class="offline">Not connected to Bernstein</div>';

  const alertsHtml =
    (data?.alerts ?? [])
      .map((a) => `<div class="alert ${a.level}">${a.message}</div>`)
      .join('') || '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Bernstein</title>
  <style nonce="${nonce}">
    body {
      font-family: var(--vscode-font-family);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      margin: 0; padding: 8px;
    }
    h3 {
      font-size: 10px; text-transform: uppercase;
      opacity: 0.6; margin: 8px 0 4px; letter-spacing: 0.5px;
    }
    .stats {
      display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 12px;
    }
    .stat {
      background: var(--vscode-editor-background);
      border-radius: 4px; padding: 6px 8px;
    }
    .label { font-size: 10px; opacity: 0.7; display: block; }
    .value { font-size: 18px; font-weight: 600; }
    .open  { color: var(--vscode-charts-blue);   }
    .running { color: var(--vscode-charts-yellow); }
    .done  { color: var(--vscode-charts-green);  }
    .failed { color: var(--vscode-charts-red);  }
    .alert {
      padding: 4px 8px; border-radius: 3px;
      font-size: 11px; margin-bottom: 4px;
    }
    .alert.warning { background: var(--vscode-inputValidation-warningBackground); }
    .alert.error   { background: var(--vscode-inputValidation-errorBackground);   }
    .offline { color: var(--vscode-disabledForeground); font-size: 12px; padding: 8px; }
  </style>
</head>
<body>
  <h3>Overview</h3>
  <div class="stats">${statsHtml}</div>
  ${alertsHtml ? `<h3>Alerts</h3>${alertsHtml}` : ''}
</body>
</html>`;
}

export class DashboardProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private data: DashboardData | null = null;

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: false };
    webviewView.webview.html = buildHtml(this.data);
  }

  update(data: DashboardData): void {
    this.data = data;
    if (this.view) {
      this.view.webview.html = buildHtml(data);
    }
  }

  /**
   * Opens the full Bernstein dashboard in the default browser.
   * VS Code webviews cannot iframe localhost due to CSP restrictions,
   * so we open externally.
   */
  static openInBrowser(baseUrl: string): void {
    void vscode.env.openExternal(vscode.Uri.parse(`${baseUrl}/dashboard`));
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add packages/vscode/src/DashboardProvider.ts
git commit -m "feat(vscode): DashboardProvider — sidebar overview + browser open"
```

---

## Task 8: Commands

**Files:**
- Create: `packages/vscode/src/commands.ts`

- [ ] **Step 1: Create `src/commands.ts`**

```typescript
import * as vscode from 'vscode';
import type { BernsteinClient } from './BernsteinClient';
import type { AgentItem } from './AgentTreeProvider';
import type { OutputManager } from './OutputManager';
import { DashboardProvider } from './DashboardProvider';

export function registerCommands(
  context: vscode.ExtensionContext,
  client: BernsteinClient,
  outputManager: OutputManager,
  onRefresh: () => void,
): void {
  context.subscriptions.push(

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

  );
}
```

- [ ] **Step 2: Commit**

```bash
git add packages/vscode/src/commands.ts
git commit -m "feat(vscode): command palette — refresh, dashboard, kill agent, show output"
```

---

## Task 9: Extension entry point

**Files:**
- Create: `packages/vscode/src/extension.ts`

- [ ] **Step 1: Create `src/extension.ts`**

```typescript
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

  const refresh = async (): Promise<void> => {
    try {
      const data = await client.getDashboardData();
      taskProvider.update(data.tasks);
      agentProvider.update(data.agents);
      statusBar.update(data);
      dashboardProvider.update(data);
    } catch (e) {
      statusBar.setError(String(e));
    }
  };

  // Initial fetch + polling fallback
  void refresh();
  const timer = setInterval(() => void refresh(), refreshIntervalSecs * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  // SSE real-time updates
  stopSse = client.subscribeToEvents(
    (event, rawData) => {
      if (event === 'task_update' || event === 'agent_update') {
        void refresh();
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
              `- Agents active: ${data.stats.agent_count}\n` +
              `- Open tasks: ${data.stats.open}\n` +
              `- Running: ${data.stats.claimed}\n` +
              `- Done: ${data.stats.done}\n` +
              `- Total cost: $${data.stats.total_cost_usd.toFixed(2)}`,
            );
          } else if (q.startsWith('cost')) {
            stream.markdown(
              `**Cost Summary**\n\nTotal: $${data.live_costs.total_usd.toFixed(4)}`,
            );
            if (data.live_costs.by_model) {
              const rows = Object.entries(data.live_costs.by_model)
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
```

- [ ] **Step 2: Type-check the full extension**

```bash
cd packages/vscode && npm run lint
```

Expected: No TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add packages/vscode/src/extension.ts
git commit -m "feat(vscode): extension.ts — activate/deactivate, wires all providers"
```

---

## Task 10: Build and package

- [ ] **Step 1: Run full test suite**

```bash
cd packages/vscode && npm test
```

Expected: All tests pass (BernsteinClient ×5, TaskTreeProvider ×8, AgentTreeProvider ×10 = 23 tests).

- [ ] **Step 2: Build the extension bundle**

```bash
cd packages/vscode && npm run compile
```

Expected: `dist/extension.js` created, no errors.

- [ ] **Step 3: Package as VSIX**

```bash
cd packages/vscode && npm run package
```

Expected: `bernstein-0.1.0.vsix` created.

If this fails with "Missing publisher name", add `--allow-missing-publisher` flag:

```bash
cd packages/vscode && npm run compile && npx vsce package --no-dependencies --allow-missing-publisher
```

- [ ] **Step 4: Install and smoke-test locally**

```bash
code --install-extension packages/vscode/bernstein-0.1.0.vsix
```

Then in VS Code:
1. Look for musical note icon in Activity Bar
2. `Ctrl+Shift+P` → "Bernstein: Show Dashboard" → browser should open `http://127.0.0.1:8052/dashboard`
3. Status bar shows `$(music) Bernstein: connecting…` (or live data if server is running)

- [ ] **Step 5: Commit**

```bash
git add packages/vscode/
git commit -m "feat(vscode): build verified — VSIX packages successfully"
```

---

## Task 11: CI/CD workflow

**Files:**
- Create: `.github/workflows/publish-extension.yml`

- [ ] **Step 1: Create `.github/workflows/publish-extension.yml`**

```yaml
name: Publish VS Code Extension

on:
  push:
    tags:
      - 'vscode-v*'   # trigger: git tag vscode-v0.1.0 && git push --tags

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
          cache-dependency-path: packages/vscode/package-lock.json

      - name: Install dependencies
        working-directory: packages/vscode
        run: npm ci

      - name: Type check
        working-directory: packages/vscode
        run: npx tsc --noEmit

      - name: Run tests
        working-directory: packages/vscode
        run: npm test

      - name: Build extension
        working-directory: packages/vscode
        run: npm run compile

      - name: Package extension
        working-directory: packages/vscode
        run: npx vsce package --no-dependencies

      # Requires VSCE_PAT secret from marketplace.visualstudio.com/manage
      - name: Publish to VS Code Marketplace
        if: ${{ secrets.VSCE_PAT != '' }}
        working-directory: packages/vscode
        run: npx vsce publish --no-dependencies --packagePath *.vsix
        env:
          VSCE_PAT: ${{ secrets.VSCE_PAT }}

      # Requires OVSX_PAT secret — Cursor/VSCodium/Gitpod use Open VSX
      - name: Publish to Open VSX
        if: ${{ secrets.OVSX_PAT != '' }}
        working-directory: packages/vscode
        run: npx ovsx publish --packagePath *.vsix
        env:
          OVSX_PAT: ${{ secrets.OVSX_PAT }}

      - name: Upload VSIX artifact
        uses: actions/upload-artifact@v4
        with:
          name: bernstein-vscode-${{ github.ref_name }}
          path: packages/vscode/*.vsix
          retention-days: 30
```

- [ ] **Step 2: Add publishing setup instructions as code comments**

The workflow already contains inline instructions (`# Requires ...`). Verify they're clear.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/publish-extension.yml
git commit -m "ci: publish VS Code extension to Marketplace + Open VSX on vscode-v* tag"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Implemented in |
|---|---|
| Activity Bar icon (musical note) | Task 1 — `bernstein-icon.svg` + `viewsContainers` |
| Agent tree view with status/model/cost | Task 4 — `AgentTreeProvider` |
| Task tree view with status/role/progress | Task 3 — `TaskTreeProvider` |
| Status bar: agents, tasks, cost | Task 5 — `StatusBarManager` |
| Commands: Refresh, Show Dashboard, Kill, Show Output | Task 8 — `commands.ts` |
| SSE real-time updates | Task 9 — `extension.ts` `subscribeToEvents` |
| Chat participant `@bernstein` | Task 9 — `extension.ts` chat block |
| Sidebar webview (Overview panel) | Task 7 — `DashboardProvider` |
| CI/CD: Marketplace + Open VSX | Task 11 — `publish-extension.yml` |
| Works in Cursor (Open VSX) | Task 11 — `ovsx publish` step |
| `bernstein.apiToken` config | Task 1 — `package.json` configuration |

### Items requiring manual setup before first publish

1. **Create VS Code publisher**: `marketplace.visualstudio.com/manage` → create publisher `bernstein`
2. **Update `package.json` repository URL**: Replace `YOUR_ORG` with actual org
3. **Add GitHub secrets**: `VSCE_PAT` (VS Code Marketplace), `OVSX_PAT` (Open VSX)
4. **Icon format**: VS Code Marketplace requires a 128×128 PNG. SVG works in the editor but convert to PNG for publishing: `npx svgexport media/bernstein-icon.svg media/bernstein-icon.png 128:128`
5. **First publish must be manual**: `vsce publish` from local machine before CI can publish updates

### Type consistency

- `BernsteinAgent.status` — used in `AgentTreeProvider` as string comparison against `'active'`/`'busy'` — matches API response field names from `dashboard/data`
- `BernsteinTask.status` — same, matches API
- `DashboardData.agents` — the API returns agents from `agents.json` when store is empty (recent fix 3c4b94f) — no change needed
- `client.baseUrl` — exposed as `readonly` so `commands.ts` can pass it to `DashboardProvider.openInBrowser()`

---

**Plan complete.** Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks

**2. Inline Execution** — execute tasks in this session using executing-plans

Which approach?
