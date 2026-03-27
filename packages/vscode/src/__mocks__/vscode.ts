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
