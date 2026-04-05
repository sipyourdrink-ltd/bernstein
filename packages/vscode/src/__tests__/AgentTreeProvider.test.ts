import { AgentItem, AgentTreeProvider } from '../AgentTreeProvider';
import type { BernsteinAgent } from '../BernsteinClient';

const BASE_AGENT: BernsteinAgent = {
  id: 'backend-abc123def456',
  role: 'backend',
  status: 'working',
  cost_usd: 0.12,
  runtime_s: 125,
  model: 'sonnet',
};

describe('AgentItem', () => {
  it('sets contextValue to agent.active when status is working', () => {
    const item = new AgentItem(BASE_AGENT);
    expect(item.contextValue).toBe('agent.active');
  });

  it('sets contextValue to agent.active when status is starting', () => {
    const item = new AgentItem({ ...BASE_AGENT, status: 'starting' });
    expect(item.contextValue).toBe('agent.active');
  });

  it('sets contextValue to agent.idle when not working or starting', () => {
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

  it('shows agent_source in description when present', () => {
    const item = new AgentItem({ ...BASE_AGENT, agent_source: 'claude' });
    expect(item.description).toContain('[claude]');
  });

  it('does not show source bracket when agent_source is absent', () => {
    const item = new AgentItem(BASE_AGENT);
    expect(item.description).not.toContain('[');
  });

  it('shows owned_files count in description', () => {
    const item = new AgentItem({
      ...BASE_AGENT,
      owned_files: ['src/a.ts', 'src/b.ts', 'src/c.ts'],
    });
    expect(item.description).toContain('(3 files)');
  });

  it('does not show file count when owned_files is empty', () => {
    const item = new AgentItem({ ...BASE_AGENT, owned_files: [] });
    expect(item.description).not.toContain('files');
  });

  it('does not show file count when owned_files is undefined', () => {
    const item = new AgentItem(BASE_AGENT);
    expect(item.description).not.toContain('files');
  });

  it('includes owned files in tooltip (truncated to 5)', () => {
    const files = ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts', 'g.ts'];
    const item = new AgentItem({ ...BASE_AGENT, owned_files: files });
    expect(item.tooltip).toContain('Owned files (7):');
    expect(item.tooltip).toContain('a.ts');
    expect(item.tooltip).toContain('e.ts');
    expect(item.tooltip).toContain('+2 more');
    expect(item.tooltip).not.toContain('f.ts');
  });

  it('includes agent_source in tooltip when present', () => {
    const item = new AgentItem({ ...BASE_AGENT, agent_source: 'gemini' });
    expect(item.tooltip).toContain('Source: gemini');
  });

  it('assigns source-specific icon when agent_source is set', () => {
    const item = new AgentItem({ ...BASE_AGENT, agent_source: 'claude' });
    // The mock ThemeIcon stores id as a property
    expect(item.iconPath).toBeDefined();
    expect((item.iconPath as { id: string }).id).toBe('sparkle');
  });

  it('uses gear icon for unknown agent_source', () => {
    const item = new AgentItem({ ...BASE_AGENT, agent_source: 'unknown-tool' });
    expect(item.iconPath).toBeDefined();
    expect((item.iconPath as { id: string }).id).toBe('gear');
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
