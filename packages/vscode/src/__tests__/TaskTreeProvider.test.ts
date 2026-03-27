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
    const item = new TaskItem({ ...BASE_TASK, progress: 42 });
    expect(item.description).toContain('42%');
  });

  it('shows agent id prefix in description when present', () => {
    const item = new TaskItem({ ...BASE_TASK, assigned_agent: 'backend-abc123def456' });
    expect(item.description).toContain('backend-abc');
  });

  it('shows only role in description when no agent', () => {
    const item = new TaskItem(BASE_TASK);
    expect(item.description).toBe('backend');
  });

  it('does not show 0% progress', () => {
    const item = new TaskItem({ ...BASE_TASK, progress: 0 });
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
