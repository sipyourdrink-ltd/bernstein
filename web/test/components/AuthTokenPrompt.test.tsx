// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { AuthTokenPrompt } from '@/components/AuthTokenPrompt';
import { AUTHORIZED_EVENT, UNAUTHORIZED_EVENT, getAuthToken } from '@/lib/api';

afterEach(() => {
  cleanup();
  localStorage.clear();
});

it('retries with a pasted token and leaves recovery visible until authentication succeeds', () => {
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <AuthTokenPrompt />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  expect(screen.queryByRole('alert')).toBeNull();

  act(() => window.dispatchEvent(new Event(UNAUTHORIZED_EVENT)));
  const input = screen.getByLabelText('Access token');
  const save = screen.getByRole<HTMLButtonElement>('button', { name: 'Save' });
  expect(save.disabled).toBe(true);
  fireEvent.change(input, { target: { value: '  test-token  ' } });
  fireEvent.click(save);
  expect(getAuthToken()).toBe('test-token');
  expect(invalidate).toHaveBeenCalledOnce();
  expect(screen.getByRole('alert').textContent).toMatch(/retrying/i);

  act(() => window.dispatchEvent(new Event(UNAUTHORIZED_EVENT)));
  expect(screen.getByRole('alert').textContent).toMatch(/rejected that token/i);

  act(() => window.dispatchEvent(new Event(AUTHORIZED_EVENT)));
  expect(screen.queryByRole('alert')).toBeNull();
});
