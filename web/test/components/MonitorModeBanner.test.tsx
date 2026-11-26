// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { MonitorModeBanner } from '@/components/MonitorModeBanner';
import Tasks from '@/routes/Tasks';

vi.mock('@/lib/sse', () => ({
  useEventStream: () => undefined,
}));

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it('renders the monitor-only banner only when runner_active is false', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <MonitorModeBanner runnerActive={null} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  expect(screen.queryByRole('complementary', { name: 'Monitor-only mode' })).toBeNull();

  rerender(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <MonitorModeBanner runnerActive={true} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  expect(screen.queryByRole('complementary', { name: 'Monitor-only mode' })).toBeNull();

  rerender(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <MonitorModeBanner runnerActive={false} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  const banner = screen.getByRole('complementary', { name: 'Monitor-only mode' });
  expect(banner.textContent).toContain('No active task runner is attached to this server.');
  expect(banner.textContent).toContain('bernstein demo');
  expect(banner.textContent).toContain('bernstein run');
  expect(banner.textContent).toContain('bernstein -g "<goal>"');
});

it('renders honest CLI guidance instead of empty-state Create task when runner_active is false', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes('/gui-meta')) {
      return jsonResponse({ version: 'dev', commit: 'abc1234', build_time: '', runner_active: false });
    }
    return jsonResponse({ items: [], total: 0, page: 1, page_size: 50, counts: {} });
  });

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Tasks />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  await screen.findByText('No tasks recorded');
  expect(screen.queryByText('No tasks yet')).toBeNull();
  expect(screen.getAllByRole('button', { name: 'Create task' })).toHaveLength(1);
  expect(screen.getByText('bernstein demo')).toBeTruthy();
  expect(screen.getByText('bernstein run')).toBeTruthy();
  expect(screen.getByText('bernstein -g "<goal>"')).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: /^Failed/ }));
  await screen.findByText('No matching tasks');
  expect(screen.getByRole('button', { name: 'Clear filters' })).toBeTruthy();
});

it('keeps the primary Create task empty state when runner_active is true', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes('/gui-meta')) {
      return jsonResponse({ version: 'dev', commit: 'abc1234', build_time: '', runner_active: true });
    }
    return jsonResponse({ items: [], total: 0, page: 1, page_size: 50, counts: {} });
  });

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Tasks />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  await screen.findByText('No tasks yet');
  expect(screen.getAllByRole('button', { name: 'Create task' })).toHaveLength(2);
});
