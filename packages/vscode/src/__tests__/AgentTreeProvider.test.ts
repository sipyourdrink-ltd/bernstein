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
